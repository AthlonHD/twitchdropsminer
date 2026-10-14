import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth import browser_login
from src.auth.browser_login import BrowserLoginCancelled, BrowserLoginManager, BrowserLoginTimeout
from tests.fake_devtools import FakeDevTools


def _fake_proc():
    proc = MagicMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestRemoteMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patch = patch.object(browser_login, "_detect_real_display", return_value=None)
        self.patch.start()
        self.manager = BrowserLoginManager()

    async def asyncTearDown(self):
        await self.manager.stop()
        self.patch.stop()

    async def test_headless_host_starts_remote_mode_without_any_browser(self):
        with patch("src.auth.plain_chrome.asyncio.create_subprocess_exec") as spawn:
            self.assertEqual(await self.manager.start(), "remote")
        spawn.assert_not_called()
        self.assertTrue(self.manager.in_progress)

    async def test_helper_submission_completes_the_wait(self):
        await self.manager.start()
        waiter = asyncio.ensure_future(self.manager.wait_for_cookie(timeout=5))
        await asyncio.sleep(0)
        await self.manager.inject_session_from_helper("tok", "dev")
        self.assertEqual(await waiter, {"auth-token": "tok", "unique_id": "dev"})

    async def test_times_out_without_a_submission(self):
        await self.manager.start()
        with patch.object(browser_login, "COOKIE_POLL_INTERVAL_SEC", 0.05):
            with self.assertRaises(BrowserLoginTimeout):
                await self.manager.wait_for_cookie(timeout=0.2)

    async def test_cancel_raises_cancelled(self):
        await self.manager.start()
        self.manager.cancel()
        with self.assertRaises(BrowserLoginCancelled):
            await self.manager.wait_for_cookie(timeout=5)

    async def test_inject_without_a_session_raises(self):
        with self.assertRaises(RuntimeError):
            await self.manager.inject_session_from_helper("tok", "")


class TestLocalMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.devtools = FakeDevTools()
        port = await self.devtools.start()
        self.proc = _fake_proc()
        self.patches = [
            patch.object(browser_login, "_detect_real_display", return_value=0),
            patch.object(browser_login, "_plain_chrome_binary", new=AsyncMock(return_value="chrome.exe")),
            patch("src.auth.plain_chrome.free_port", return_value=port),
            patch("src.auth.plain_chrome.asyncio.create_subprocess_exec", new=AsyncMock(return_value=self.proc)),
            patch.object(browser_login, "COOKIE_POLL_INTERVAL_SEC", 0.05),
        ]
        for p in self.patches:
            p.start()
        self.manager = BrowserLoginManager()

    async def asyncTearDown(self):
        await self.manager.stop()
        for p in self.patches:
            p.stop()
        await self.devtools.stop()

    async def test_desktop_host_opens_the_login_page_in_a_plain_chrome(self):
        with patch("src.auth.plain_chrome.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=self.proc)) as spawn:
            self.assertEqual(await self.manager.start(), "local")
        self.assertEqual(spawn.await_args.args[0], "chrome.exe")
        self.assertEqual(spawn.await_args.args[-1], browser_login.LOGIN_URL)

    async def test_returns_the_cookie_once_the_user_logged_in(self):
        await self.manager.start()
        waiter = asyncio.ensure_future(self.manager.wait_for_cookie(timeout=5))
        await asyncio.sleep(0.1)
        self.devtools.cookies = [
            {"name": "auth-token", "value": "tok", "domain": ".twitch.tv"},
            {"name": "unique_id", "value": "dev", "domain": ".twitch.tv"},
        ]
        self.assertEqual(await waiter, {"auth-token": "tok", "unique_id": "dev"})

    async def test_local_window_closed_counts_as_cancel(self):
        await self.manager.start()
        await self.devtools.close_browser()
        with self.assertRaises(BrowserLoginCancelled):
            await self.manager.wait_for_cookie(timeout=5)

    async def test_helper_submission_also_works_in_local_mode(self):
        await self.manager.start()
        await self.manager.inject_session_from_helper("tok", "")
        self.assertEqual(await self.manager.wait_for_cookie(timeout=5), {"auth-token": "tok", "unique_id": ""})

    async def test_stop_closes_the_browser(self):
        await self.manager.start()
        await self.manager.stop()
        self.assertIn("Browser.close", self.devtools.calls)
        self.assertFalse(self.manager.in_progress)

    async def test_start_twice_raises(self):
        await self.manager.start()
        with self.assertRaises(RuntimeError):
            await self.manager.start()


class TestLocalFallback(unittest.IsolatedAsyncioTestCase):
    async def test_local_falls_back_to_remote_when_chrome_cannot_start(self):
        manager = BrowserLoginManager()
        with (
            patch.object(browser_login, "_detect_real_display", return_value=0),
            patch.object(browser_login, "_plain_chrome_binary", new=AsyncMock(side_effect=RuntimeError("no chrome"))),
        ):
            self.assertEqual(await manager.start(), "remote")
        await manager.stop()
