import asyncio
import base64
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.browser_login import (
    _decode_jwt_expiry,
    _session_cookies,
    acquire_integrity_token,
)


def _fake_jwt(exp: int | None) -> str:
    payload = {} if exp is None else {"exp": exp}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


class TestDecodeJwtExpiry(unittest.TestCase):
    """
    Client-Integrity tokens are now minted by a real Chromium session
    (see acquire_integrity_token) and their own JWT "exp" claim is the
    only reliable signal for when to renew -- this replaces the old
    streamlink-based module's own expiry handling.
    """

    def test_reads_the_real_exp_claim(self):
        exp = int(time.time()) + 7200
        expiry = _decode_jwt_expiry(_fake_jwt(exp))
        self.assertAlmostEqual(expiry.timestamp(), exp, delta=1)

    def test_falls_back_to_a_default_ttl_when_not_a_jwt_at_all(self):
        before = datetime.now(timezone.utc)
        expiry = _decode_jwt_expiry("not-a-jwt-token")
        self.assertGreater(expiry, before + timedelta(hours=3))

    def test_falls_back_to_a_default_ttl_when_exp_claim_is_missing(self):
        before = datetime.now(timezone.utc)
        expiry = _decode_jwt_expiry(_fake_jwt(None))
        self.assertGreater(expiry, before + timedelta(hours=3))

    def test_falls_back_to_a_default_ttl_on_malformed_base64(self):
        before = datetime.now(timezone.utc)
        expiry = _decode_jwt_expiry("header.!!!not-base64!!!.sig")
        self.assertGreater(expiry, before + timedelta(hours=3))


class TestSessionCookies(unittest.TestCase):
    """The cookies a plain Chrome is seeded with before minting a token."""

    def test_auth_token_is_readable_by_the_page(self):
        # An httpOnly auth-token leaves Twitch's page anonymous (confirmed
        # live: /integrity then goes out without Authorization and the
        # resulting token fails dropCampaign).
        cookies = {c["name"]: c for c in _session_cookies("token123", "device-abc")}
        self.assertEqual(set(cookies), {"auth-token", "unique_id"})
        self.assertFalse(cookies["auth-token"].get("httpOnly", False))

    def test_skips_the_device_id_cookie_when_blank(self):
        names = {c["name"] for c in _session_cookies("token123", "")}
        self.assertEqual(names, {"auth-token"})


class TestAcquireIntegrityToken(unittest.IsolatedAsyncioTestCase):
    """
    The token is minted in a plain, non-Playwright Chrome (see
    _mint_integrity_token_plain): one minted in a Playwright-driven browser
    fails Twitch's integrity check. These never launch a real browser.
    """

    async def test_returns_the_minted_token_on_a_real_display(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        mint = AsyncMock(return_value=("itok", expiry))
        with (
            patch("src.auth.browser_login._detect_real_display", return_value=7),
            patch("src.auth.browser_login._plain_chrome_binary", new=AsyncMock(return_value="chrome")),
            patch("src.auth.browser_login._mint_integrity_token_plain", new=mint),
        ):
            result = await acquire_integrity_token("token123", "device-abc")

        self.assertEqual(result, ("itok", expiry))
        binary, env, auth_token, device_id = mint.await_args.args
        self.assertEqual((binary, auth_token, device_id), ("chrome", "token123", "device-abc"))
        self.assertEqual(env["DISPLAY"], ":7")

    async def test_none_when_minting_fails(self):
        with (
            patch("src.auth.browser_login._detect_real_display", return_value=7),
            patch("src.auth.browser_login._plain_chrome_binary", new=AsyncMock(return_value="chrome")),
            patch(
                "src.auth.browser_login._mint_integrity_token_plain",
                new=AsyncMock(side_effect=RuntimeError("Chrome debug endpoint never came up")),
            ),
        ):
            result = await acquire_integrity_token("token123", "device-abc")
        self.assertIsNone(result)

    async def test_tears_down_xvfb_when_it_spawned_one(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        xvfb_proc = MagicMock()

        with (
            patch("src.auth.browser_login._detect_real_display", return_value=None),
            patch("src.auth.browser_login._find_free_display_number", return_value=92),
            patch(
                "src.auth.browser_login._start_tagged_process",
                new=AsyncMock(return_value=xvfb_proc),
            ),
            patch("src.auth.browser_login._wait_for_display", new=AsyncMock()),
            patch("src.auth.browser_login._plain_chrome_binary", new=AsyncMock(return_value="chrome")),
            patch(
                "src.auth.browser_login._mint_integrity_token_plain",
                new=AsyncMock(return_value=("itok", expiry)),
            ),
            patch(
                "src.auth.browser_login._terminate_process_group_gracefully", new=AsyncMock()
            ) as mock_terminate,
            patch("src.auth.browser_login._release_display_number") as mock_release,
        ):
            result = await acquire_integrity_token("token123", "device-abc")

        self.assertEqual(result, ("itok", expiry))
        mock_terminate.assert_awaited_once_with(xvfb_proc)
        mock_release.assert_called_once_with(92)

