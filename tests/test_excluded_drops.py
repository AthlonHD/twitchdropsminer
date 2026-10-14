"""Blacklisted drops and ignored campaigns still gain minutes (Twitch credits
every running campaign of the watched game), but TDM must neither present
them as the drop it is mining nor claim them."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.models.campaign import DropsCampaign


def _drop(drop_id, name, minutes, now):
    return {
        "id": drop_id,
        "name": name,
        "benefitEdges": [
            {
                "benefit": {
                    "id": f"{drop_id}-benefit",
                    "name": name,
                    "distributionType": "DIRECT_ENTITLEMENT",
                    "imageAssetURL": "http://img",
                }
            }
        ],
        "startAt": (now - timedelta(hours=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "requiredMinutesWatched": minutes,
        "preconditionDrops": [],
    }


def _campaign(*, blacklisted_ids=(), name_blacklist=(), ignored_ids=(), chain=False):
    now = datetime.now(timezone.utc)
    twitch = MagicMock()
    twitch.settings.blacklisted_drop_ids = list(blacklisted_ids)
    twitch.settings.drop_name_blacklist = list(name_blacklist)
    twitch.settings.ignored_campaign_ids = list(ignored_ids)
    data = {
        "id": "camp",
        "name": "Community Day",
        "game": {"id": "1", "name": "Hearthstone", "displayName": "Hearthstone",
                 "boxArtURL": "https://example.test/{width}x{height}.jpg"},
        "self": {"isAccountConnected": True},
        "accountLinkURL": "https://example.test/link",
        "startAt": (now - timedelta(hours=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "status": "ACTIVE",
        "allow": {"channels": [], "isEnabled": True},
        # The unwanted drop finishes first, so it used to win first_drop.
        "timeBasedDrops": [_drop("ticket", "Tavern Ticket", 10, now), _drop("pack", "Standard Pack", 60, now)],
    }
    if chain:
        # ticket -> key -> pack: the wanted pack unlocks only once both
        # earlier drops are claimed.
        key = _drop("key", "Tavern Key", 20, now)
        key["preconditionDrops"] = [{"id": "ticket"}]
        data["timeBasedDrops"][1]["preconditionDrops"] = [{"id": "key"}]
        data["timeBasedDrops"].append(key)
    return DropsCampaign(twitch, data, {})


def _by_id(campaign, drop_id):
    return next(d for d in campaign.drops if d.id == drop_id)


class TestExcludedProperty(unittest.TestCase):
    def test_nothing_excluded_by_default(self):
        self.assertFalse(any(d.excluded for d in _campaign().drops))

    def test_blacklisted_id(self):
        campaign = _campaign(blacklisted_ids=["ticket"])
        self.assertTrue(_by_id(campaign, "ticket").excluded)
        self.assertFalse(_by_id(campaign, "pack").excluded)

    def test_name_blacklist_is_case_insensitive_keyword(self):
        self.assertTrue(_by_id(_campaign(name_blacklist=["tavern"]), "ticket").excluded)

    def test_ignored_campaign_excludes_all_its_drops(self):
        self.assertTrue(all(d.excluded for d in _campaign(ignored_ids=["camp"]).drops))


class TestPreconditionsOfWantedDropsStayClaimable(unittest.TestCase):
    def test_blacklisted_drop_that_unlocks_a_wanted_one_is_not_excluded(self):
        campaign = _campaign(blacklisted_ids=["ticket", "key"], chain=True)
        self.assertFalse(_by_id(campaign, "ticket").excluded)
        self.assertFalse(_by_id(campaign, "key").excluded)

    def test_chain_ending_in_unwanted_drops_stays_excluded(self):
        campaign = _campaign(blacklisted_ids=["ticket", "key", "pack"], chain=True)
        self.assertTrue(all(d.excluded for d in campaign.drops))


class TestFirstDropSkipsExcluded(unittest.TestCase):
    def test_blacklisted_drop_is_not_shown_as_the_mined_drop(self):
        campaign = _campaign(blacklisted_ids=["ticket"])
        self.assertEqual(campaign.first_drop.id, "pack")

    def test_without_blacklist_the_quickest_drop_wins(self):
        self.assertEqual(_campaign().first_drop.id, "ticket")


class TestClaimSkipsExcluded(unittest.IsolatedAsyncioTestCase):
    async def test_blacklisted_drop_is_not_claimed(self):
        drop = _by_id(_campaign(blacklisted_ids=["ticket"]), "ticket")
        drop.claim_id = "user#camp#ticket"
        drop._claim = AsyncMock(return_value=True)

        self.assertFalse(await drop.claim())
        drop._claim.assert_not_called()
        self.assertFalse(drop.is_claimed)

    async def test_ignored_campaign_drop_is_not_claimed(self):
        drop = _by_id(_campaign(ignored_ids=["camp"]), "pack")
        drop.claim_id = "user#camp#pack"
        drop._claim = AsyncMock(return_value=True)

        self.assertFalse(await drop.claim())
        drop._claim.assert_not_called()

    async def test_wanted_drop_is_still_claimed(self):
        drop = _by_id(_campaign(blacklisted_ids=["ticket"]), "pack")
        drop.claim_id = "user#camp#pack"
        drop._claim = AsyncMock(return_value=True)

        self.assertTrue(await drop.claim())
        drop._claim.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
