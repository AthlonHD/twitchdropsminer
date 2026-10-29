import asyncio
import unittest
from unittest.mock import AsyncMock

from integrations.hdtool.remote_login import RemoteBrowser, RemoteLoginManager


class FakeBrowser:
    def __init__(self):
        self.start = AsyncMock()
        self.close = AsyncMock()


class RemoteLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_browser_per_user_and_global_capacity(self):
        manager = RemoteLoginManager(FakeBrowser)
        alice = await manager.open("alice")
        bob = await manager.open("bob")
        with self.assertRaises(RuntimeError):
            await manager.open("alice")
        with self.assertRaises(RuntimeError):
            await manager.open("charlie")
        await manager.close("alice", alice)
        charlie = await manager.open("charlie")
        self.assertIsNot(bob, charlie)
        await manager.shutdown()
        self.assertEqual(manager.sessions, {})
        alice.close.assert_awaited_once()

    async def test_start_failure_cleans_up(self):
        browser = FakeBrowser()
        browser.start.side_effect = RuntimeError("startup failed")
        manager = RemoteLoginManager(lambda: browser)
        with self.assertRaises(RuntimeError):
            await manager.open("alice")
        self.assertEqual(manager.sessions, {})
        browser.close.assert_awaited_once()

    async def test_client_cannot_send_arbitrary_devtools_commands_or_shortcuts(self):
        browser = RemoteBrowser()
        browser.call = AsyncMock()
        for data in ({"type": "navigate", "url": "http://127.0.0.1:8765"}, {"type": "key", "key": "F12"},
                     {"type": "key", "key": "l", "ctrl": True}, {"type": "text", "text": "x" * 2001},
                     {"type": "down", "x": -1, "y": 10}):
            with self.assertRaises(ValueError):
                await browser.input(data)
        browser.call.assert_not_awaited()
        await browser.input({"type": "text", "text": "sample"})
        browser.call.assert_awaited_with("Input.insertText", {"text": "sample"})
