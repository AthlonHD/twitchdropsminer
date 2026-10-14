"""A tiny stand-in for Chrome's DevTools endpoint: /json/version plus a
browser WebSocket that answers CDP calls. Used instead of a real Chrome."""
from __future__ import annotations

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer


class FakeDevTools:
    def __init__(self) -> None:
        self.cookies: list[dict] = []
        self.calls: list[str] = []
        self._ws: web.WebSocketResponse | None = None
        app = web.Application()
        app.router.add_get("/json/version", self._version)
        app.router.add_get("/devtools/browser/fake", self._socket)
        self._server = TestServer(app, host="127.0.0.1")

    async def start(self) -> int:
        await self._server.start_server()
        return self._server.port

    async def stop(self) -> None:
        await self._server.close()

    async def close_browser(self) -> None:
        """Simulate the user closing the Chrome window."""
        if self._ws is not None:
            await self._ws.close()

    async def _version(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self._server.port}/devtools/browser/fake"}
        )

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            data = msg.json()
            self.calls.append(data["method"])
            result: dict = {}
            if data["method"] == "Storage.getCookies":
                result = {"cookies": self.cookies}
            await ws.send_json({"id": data["id"], "result": result})
            if data["method"] == "Browser.close":
                await ws.close()
        return ws
