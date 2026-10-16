"""A normal Chrome process observed only through its DevTools port.

Twitch detects Playwright-driven browsers: its login page answers "Your
browser is not currently supported" and integrity tokens minted there are
rejected. A Chrome started as an ordinary process with
--remote-debugging-port, read only through the browser target (cookies) or
the Network/Page domains -- never Runtime / JS injection -- is not flagged
(confirmed live 2026-10-14 on Windows, x86 Linux Docker and a VPS).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import tempfile

import aiohttp


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PlainChrome:
    def __init__(
        self, binary: str, url: str, env: dict[str, str] | None = None, *, no_sandbox: bool = False
    ) -> None:
        self._binary = binary
        self._url = url
        self._env = env
        self._no_sandbox = no_sandbox
        self._proc: asyncio.subprocess.Process | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._profile_dir: str | None = None
        self._next_id = 0
        self._events: list[dict] = []

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._ws is not None
            and not self._ws.closed
        )

    async def start(self, timeout: float = 30.0) -> None:
        port = free_port()
        self._profile_dir = tempfile.mkdtemp(prefix="tdm-chrome-")
        args = [
            self._binary,
            *(["--no-sandbox"] if self._no_sandbox else []),
            f"--user-data-dir={self._profile_dir}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            self._url,
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *args, env=self._env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        self._http = aiohttp.ClientSession()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                async with self._http.get(f"http://127.0.0.1:{port}/json/version") as resp:
                    ws_url = (await resp.json())["webSocketDebuggerUrl"]
                    break
            except aiohttp.ClientError:
                if self._proc.returncode is not None or loop.time() >= deadline:
                    await self.close()
                    raise RuntimeError("Chrome debug endpoint never came up")
                await asyncio.sleep(0.5)
        self._ws = await self._http.ws_connect(ws_url, max_msg_size=0)

    async def call(
        self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float = 30.0
    ) -> dict:
        assert self._ws is not None, "start() first"
        self._next_id += 1
        msg: dict = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send_json(msg)
        while True:
            data = await self._ws.receive_json(timeout=timeout)
            if data.get("id") == msg["id"]:
                return data
            self._events.append(data)

    async def next_event(self, timeout: float) -> dict:
        if self._events:
            return self._events.pop(0)
        assert self._ws is not None, "start() first"
        return await self._ws.receive_json(timeout=timeout)

    async def cookies(self, domain_suffix: str) -> dict[str, str]:
        reply = await self.call("Storage.getCookies")
        return {
            c["name"]: c["value"]
            for c in reply.get("result", {}).get("cookies", [])
            if c["domain"].endswith(domain_suffix)
        }

    async def close(self) -> None:
        ws, http, proc, profile_dir = self._ws, self._http, self._proc, self._profile_dir
        self._ws = self._http = self._profile_dir = None
        if ws is not None and not ws.closed:
            try:
                self._next_id += 1
                await ws.send_json({"id": self._next_id, "method": "Browser.close", "params": {}})
            except Exception:
                pass
            await ws.close()
        if http is not None:
            await http.close()
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if profile_dir is not None:
            shutil.rmtree(profile_dir, ignore_errors=True)
