import unittest
from unittest.mock import MagicMock, patch

from src.web.managers.campaigns import CampaignProgressManager


class TestMinuteAlmostDone(unittest.TestCase):
    """The watch loop polls CurrentDrop only when minute_almost_done() is true.
    remaining_seconds is the drop's whole remaining time and is never counted
    down, so the poll must also fire once ~a minute passed without an update."""

    def _manager(self):
        manager = CampaignProgressManager(MagicMock())
        with patch("src.web.managers.campaigns.asyncio.create_task"):
            manager.update(None, 26 * 60)
        return manager

    def test_not_done_right_after_an_update(self):
        with patch("src.web.managers.campaigns.time.monotonic", return_value=1000.0):
            manager = self._manager()
            self.assertFalse(manager.minute_almost_done())

    def test_done_once_a_minute_passed_without_an_update(self):
        with patch("src.web.managers.campaigns.time.monotonic", return_value=1000.0):
            manager = self._manager()
        with patch("src.web.managers.campaigns.time.monotonic", return_value=1060.0):
            self.assertTrue(manager.minute_almost_done())

    def test_done_before_any_update(self):
        self.assertTrue(CampaignProgressManager(MagicMock()).minute_almost_done())
