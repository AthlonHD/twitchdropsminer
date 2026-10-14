import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.auth_state import _AuthState


class TestBrowserLoginIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_browser_login_returns_access_token_from_manager(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login_remote = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        # _browser_login() does `from src.auth.browser_login import
        # BrowserLoginManager` INSIDE the method (a local import, to avoid
        # a circular import between auth_state.py and browser_login.py) --
        # patch it at its origin (src.auth.browser_login), not at
        # src.auth.auth_state, since that name is never a module-level
        # attribute of auth_state.py. The local import re-resolves the
        # module's current attribute at call time, so patching the origin
        # before calling _browser_login() works correctly.
        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value="remote")
            instance.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "real-token-value", "unique_id": "device-abc"}
            )
            instance.stop = AsyncMock()
            instance.captured_integrity_token = AsyncMock(return_value=None)

            token = await auth_state._browser_login()

            self.assertEqual(token, "real-token-value")
            instance.start.assert_awaited_once()
            instance.wait_for_cookie.assert_awaited_once()
            instance.stop.assert_awaited_once()

    async def test_browser_login_does_not_adopt_the_login_pages_token(self):
        # A token captured in the Playwright-driven login browser fails
        # Twitch's integrity check; _ensure_integrity_token mints a usable
        # one in a plain Chrome right after login instead.
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login_remote = AsyncMock()
        auth_state = _AuthState(mock_twitch)
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)

        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value="remote")
            instance.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "real-token-value", "unique_id": "device-abc"}
            )
            instance.stop = AsyncMock()
            instance.captured_integrity_token = AsyncMock(return_value=("itok-live", expiry))

            await auth_state._browser_login()

        self.assertIsNone(auth_state._integrity_token)

    async def test_browser_login_tells_the_dashboard_which_mode_it_is_in(self):
        for mode, expected in (("local", "start_browser_login_local"), ("remote", "start_browser_login_remote")):
            mock_twitch = MagicMock()
            mock_twitch.gui.login.start_browser_login_local = AsyncMock()
            mock_twitch.gui.login.start_browser_login_remote = AsyncMock()
            auth_state = _AuthState(mock_twitch)
            with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
                instance = MockManager.return_value
                instance.start = AsyncMock(return_value=mode)
                instance.wait_for_cookie = AsyncMock(return_value={"auth-token": "t", "unique_id": "d"})
                instance.stop = AsyncMock()
                await auth_state._browser_login()
            getattr(mock_twitch.gui.login, expected).assert_awaited_once()

    async def test_browser_login_stops_manager_even_on_failure(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login_remote = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value="remote")
            instance.wait_for_cookie = AsyncMock(side_effect=RuntimeError("timed out"))
            instance.stop = AsyncMock()

            with self.assertRaises(RuntimeError):
                await auth_state._browser_login()

            instance.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
