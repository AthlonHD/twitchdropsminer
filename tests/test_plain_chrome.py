import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.plain_chrome import PlainChrome
from tests.fake_devtools import FakeDevTools


def _fake_proc():
    proc = MagicMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestPlainChrome(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.devtools = FakeDevTools()
        self.port = await self.devtools.start()
        self.proc = _fake_proc()
        self.exec = AsyncMock(return_value=self.proc)
        self.patches = [
            patch("src.auth.plain_chrome.free_port", return_value=self.port),
            patch("src.auth.plain_chrome.asyncio.create_subprocess_exec", new=self.exec),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.devtools.stop()

    async def test_launches_a_plain_chrome_on_the_url_with_a_debug_port(self):
        chrome = PlainChrome("chrome.exe", "https://www.twitch.tv/login")
        await chrome.start()
        argv = self.exec.await_args.args
        self.assertEqual(argv[0], "chrome.exe")
        self.assertIn(f"--remote-debugging-port={self.port}", argv)
        self.assertEqual(argv[-1], "https://www.twitch.tv/login")
        self.assertNotIn("--no-sandbox", argv)
        self.assertTrue(chrome.alive)
        await chrome.close()

    async def test_no_sandbox_flag_is_opt_in(self):
        chrome = PlainChrome("chromium", "about:blank", no_sandbox=True)
        await chrome.start()
        self.assertIn("--no-sandbox", self.exec.await_args.args)
        await chrome.close()

    async def test_cookies_filters_by_domain(self):
        self.devtools.cookies = [
            {"name": "auth-token", "value": "tok", "domain": ".twitch.tv"},
            {"name": "other", "value": "x", "domain": ".example.com"},
        ]
        chrome = PlainChrome("chrome.exe", "about:blank")
        await chrome.start()
        self.assertEqual(await chrome.cookies("twitch.tv"), {"auth-token": "tok"})
        await chrome.close()

    async def test_not_alive_after_the_window_is_closed(self):
        chrome = PlainChrome("chrome.exe", "about:blank")
        await chrome.start()
        await self.devtools.close_browser()
        with self.assertRaises(Exception):
            await chrome.cookies("twitch.tv")
        self.assertFalse(chrome.alive)
        await chrome.close()

    async def test_close_is_idempotent_and_sends_browser_close(self):
        chrome = PlainChrome("chrome.exe", "about:blank")
        await chrome.start()
        await chrome.close()
        await chrome.close()
        self.assertIn("Browser.close", self.devtools.calls)

    async def test_start_fails_cleanly_when_chrome_exits_early(self):
        self.proc.returncode = 1
        with patch("src.auth.plain_chrome.free_port", return_value=1):  # nothing listens there
            chrome = PlainChrome("chrome.exe", "about:blank")
            with self.assertRaises(RuntimeError):
                await chrome.start(timeout=2)
        self.assertFalse(chrome.alive)
