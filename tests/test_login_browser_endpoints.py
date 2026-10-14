import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.auth import browser_login
from src.auth.auth_state import _AuthState
from src.auth.browser_login import BrowserLoginCancelled
from src.i18n import _
from src.web import app as app_module


def _fake_manager(mode: str = "remote") -> MagicMock:
    """A stand-in for BrowserLoginManager exposing only the public surface
    the web layer is allowed to touch."""
    manager = MagicMock()
    manager.in_progress = True
    manager.cancel = MagicMock()
    manager.start = AsyncMock(return_value=mode)
    manager.stop = AsyncMock()
    manager.inject_session_from_helper = AsyncMock()
    return manager


class TestBrowserLoginCancelEndpoint(unittest.TestCase):
    def setUp(self):
        # PasswordAuthMiddleware redirects every request to /__setup until
        # first-time setup is marked done (see _is_setup_done) -- irrelevant
        # to what's under test here, so short-circuit it.
        self._setup_patcher = patch("src.web.app._is_setup_done", return_value=True)
        self._setup_patcher.start()
        self.addCleanup(self._setup_patcher.stop)
        # These endpoints are otherwise gated by whatever password/bot-token
        # happen to be configured on the machine running the suite -- pin
        # them to "unset" so the test doesn't depend on host state.
        self._pw_patcher = patch("src.web.app._get_password", return_value="")
        self._pw_patcher.start()
        self.addCleanup(self._pw_patcher.stop)
        self._token_patcher = patch("src.web.app._get_bot_token", return_value="")
        self._token_patcher.start()
        self.addCleanup(self._token_patcher.stop)
        self.client = TestClient(app_module.app)
        self.addCleanup(browser_login.set_active_manager, None)

    def test_start_endpoint_no_longer_exists(self):
        # The auth flow is the one and only owner of a login attempt; a
        # second entry point here would start a Chromium session nobody
        # polls a cookie for.
        response = self.client.post("/api/login/browser/start")
        self.assertEqual(response.status_code, 404)

    def test_cancel_is_a_noop_when_no_attempt_is_in_progress(self):
        browser_login.set_active_manager(None)
        response = self.client.post("/api/login/browser/cancel")
        self.assertEqual(response.status_code, 200)

    def test_cancel_cancels_the_active_manager(self):
        manager = _fake_manager()
        browser_login.set_active_manager(manager)
        response = self.client.post("/api/login/browser/cancel")
        self.assertEqual(response.status_code, 200)
        manager.cancel.assert_called_once()


class TestHelperSessionEndpoint(unittest.TestCase):
    """
    For scripts/tdm_login_helper.py: logs in with the user's own real,
    already-installed browser on their own device (Twitch's bot/integrity
    check flags datacenter IPs regardless of anything this app does on its
    own server-side Chromium) and POSTs the resulting cookies here
    automatically.
    """

    def setUp(self):
        self._setup_patcher = patch("src.web.app._is_setup_done", return_value=True)
        self._setup_patcher.start()
        self.addCleanup(self._setup_patcher.stop)
        self._pw_patcher = patch("src.web.app._get_password", return_value="")
        self._pw_patcher.start()
        self.addCleanup(self._pw_patcher.stop)
        self._token_patcher = patch("src.web.app._get_bot_token", return_value="")
        self._token_patcher.start()
        self.addCleanup(self._token_patcher.stop)
        self.client = TestClient(app_module.app)
        self.addCleanup(browser_login.set_active_manager, None)

    def test_rejects_a_missing_auth_token(self):
        browser_login.set_active_manager(_fake_manager())
        response = self.client.post("/api/login/browser/helper-session", json={"auth_token": "  "})
        self.assertEqual(response.status_code, 400)

    def test_rejects_when_no_login_is_in_progress(self):
        browser_login.set_active_manager(None)
        response = self.client.post(
            "/api/login/browser/helper-session", json={"auth_token": "abc123"}
        )
        self.assertEqual(response.status_code, 409)

    def test_forwards_cookies_to_the_active_manager(self):
        manager = _fake_manager()
        browser_login.set_active_manager(manager)
        response = self.client.post(
            "/api/login/browser/helper-session",
            json={"auth_token": " abc123 ", "unique_id": " device-xyz "},
        )
        self.assertEqual(response.status_code, 200)
        manager.inject_session_from_helper.assert_awaited_once_with("abc123", "device-xyz")

    def test_unique_id_defaults_to_empty(self):
        manager = _fake_manager()
        browser_login.set_active_manager(manager)
        response = self.client.post(
            "/api/login/browser/helper-session", json={"auth_token": "abc123"}
        )
        self.assertEqual(response.status_code, 200)
        manager.inject_session_from_helper.assert_awaited_once_with("abc123", "")


class TestBrowserLoginManagerIsSharedAcrossLayers(unittest.IsolatedAsyncioTestCase):
    """
    The regression this whole file exists to prevent: the auth flow used to
    drive a LOCAL BrowserLoginManager while src/web/app.py held a separate
    module-level one that only its (now deleted) /start endpoint ever
    assigned. The result was a feature that could never work end-to-end --
    the noVNC panel opened, the WS route found no manager and closed 4404,
    and Cancel cancelled nothing while the real Chromium ran on for the full
    10-minute timeout. These tests pin the single-owner invariant.
    """

    async def asyncSetUp(self):
        self.addCleanup(browser_login.set_active_manager, None)

    async def test_auth_flow_publishes_its_manager_and_clears_it_afterwards(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login_remote = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        manager = _fake_manager()
        observed: list[object] = []
        parked = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_cookie(*args, **kwargs):
            observed.append(browser_login.get_active_manager())
            parked.set()
            await release.wait()
            raise BrowserLoginCancelled()

        manager.wait_for_cookie = AsyncMock(side_effect=wait_for_cookie)

        self.assertIsNone(browser_login.get_active_manager())

        with patch("src.auth.browser_login.BrowserLoginManager", return_value=manager):
            task = asyncio.ensure_future(auth_state._browser_login())
            await asyncio.wait_for(parked.wait(), timeout=5)

            # While the auth flow is awaiting the cookie, the module-level
            # accessor every other layer reads returns THAT manager.
            self.assertIs(browser_login.get_active_manager(), manager)
            self.assertIs(observed[0], manager)

            release.set()
            # The retry loop sleeps before offering a fresh browser; stop it
            # there. The finally block has already run by then.
            await asyncio.sleep(0)
            for _ in range(50):
                if browser_login.get_active_manager() is None:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        manager.stop.assert_awaited()
        self.assertIsNone(browser_login.get_active_manager())


class TestLoginFormManagerBrowserLoginStatus(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from src.web.managers.login import LoginFormManager

        broadcaster = MagicMock()
        broadcaster.emit = AsyncMock()
        return LoginFormManager(broadcaster, MagicMock()), broadcaster

    async def test_remote_attempt_reports_the_login_helper(self):
        manager, broadcaster = self._manager()
        await manager.start_browser_login_remote()
        self.assertEqual(manager.get_status()["login_helper"], "remote")
        payload = broadcaster.emit.await_args_list[-1].args[1]
        self.assertEqual(payload["login_helper"], "remote")

    async def test_local_attempt_also_offers_the_helper(self):
        # The local window may be on a machine the user is not sitting at
        # (dashboard opened from a phone, TDM as a Windows service).
        manager, _ = self._manager()
        await manager.start_browser_login_local()
        status = manager.get_status()
        self.assertEqual(status["login_helper"], "local")
        self.assertIn("browser window", status["status"].lower())

    async def test_local_attempt_survives_a_language_without_browser_login_strings(self):
        from src.i18n import _

        manager, _broadcaster = self._manager()
        login = {k: v for k, v in _.t["login"].items() if k != "browser_login"}
        with patch.dict(_.t, {"login": login}):
            await manager.start_browser_login_local()
        self.assertIn("browser window", manager.get_status()["status"].lower())

    async def test_a_later_status_update_clears_the_helper_again(self):
        manager, _ = self._manager()
        await manager.start_browser_login_remote()
        manager.update("Logged in", 1, "someone")
        await asyncio.sleep(0)
        self.assertNotIn("login_helper", manager.get_status())


class TestBrowserLoginFailuresDoNotKillTheApp(unittest.IsolatedAsyncioTestCase):
    """
    _browser_login() had a try/finally but no except, so a timeout, a cancel
    or a missing system dependency propagated all the way into __main__'s
    generic `except Exception` handler and exited the process. It must
    instead surface as dashboard status and offer a fresh login browser.
    """

    def setUp(self):
        self.addCleanup(browser_login.set_active_manager, None)

    async def _run_until_second_attempt(self, first_exception):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login_remote = AsyncMock()
        mock_twitch.gui.login.update = MagicMock()
        auth_state = _AuthState(mock_twitch)

        attempts = []

        async def wait_for_cookie(*args, **kwargs):
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise first_exception
            return {"auth-token": "token-from-retry", "unique_id": "device-abc"}

        def make_manager():
            manager = _fake_manager()
            manager.wait_for_cookie = AsyncMock(side_effect=wait_for_cookie)
            return manager

        with (
            patch("src.auth.browser_login.BrowserLoginManager", side_effect=make_manager),
            patch("src.auth.auth_state.BROWSER_LOGIN_RETRY_DELAY_SEC", 0),
        ):
            token = await asyncio.wait_for(auth_state._browser_login(), timeout=5)

        return token, attempts, mock_twitch.gui.login.update

    async def test_timeout_is_retried_with_a_fresh_browser(self):
        from src.auth.browser_login import BrowserLoginTimeout

        token, attempts, update = await self._run_until_second_attempt(BrowserLoginTimeout())
        self.assertEqual(token, "token-from-retry")
        self.assertEqual(len(attempts), 2)
        self.assertIn(
            _.t["login"]["status"]["timed_out"],
            [call.args[0] for call in update.call_args_list],
        )

    async def test_cancel_is_retried_with_a_fresh_browser(self):
        token, attempts, update = await self._run_until_second_attempt(BrowserLoginCancelled())
        self.assertEqual(token, "token-from-retry")
        self.assertIn(
            _.t["login"]["status"]["cancelled"],
            [call.args[0] for call in update.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()


class TestLoginHelperDownload(unittest.TestCase):
    def test_served_without_dashboard_auth_even_with_a_password(self):
        from pathlib import Path

        from starlette.testclient import TestClient

        with (
            patch.object(app_module, "_is_setup_done", return_value=True),
            patch.object(app_module, "_get_password", return_value="secret"),
        ):
            response = TestClient(app_module.app).get("/login-helper.py")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/x-python"))
        expected = (Path(app_module.__file__).resolve().parents[2] / "scripts" / "tdm_login_helper.py").read_text(encoding="utf-8")
        self.assertEqual(response.text, expected)

    def test_helper_session_post_still_needs_the_password(self):
        from starlette.testclient import TestClient

        with (
            patch.object(app_module, "_is_setup_done", return_value=True),
            patch.object(app_module, "_get_password", return_value="secret"),
        ):
            response = TestClient(app_module.app).post(
                "/api/login/browser/helper-session", json={"auth_token": "x"}
            )
        self.assertEqual(response.status_code, 401)
