"""Bounded, disposable server browsers for an optional no-install web login."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

import aiohttp


WIDTH, HEIGHT = 1100, 800
ALLOWED_KEYS = {"Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27,
                "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "Delete": 46, "Home": 36, "End": 35}


class RemoteBrowser:
    """No arbitrary URLs, JavaScript execution, DevTools commands, or host paths from users."""

    def __init__(self):
        self.process = None
        self.profile = None
        self.http = None
        self.ws = None
        self.counter = 0
        self.lock = asyncio.Lock()
        self.started = time.monotonic()

    @staticmethod
    def launch_arguments(binary, profile, port, environ=None, windows=None):
        env = os.environ if environ is None else environ
        windows = os.name == "nt" if windows is None else windows
        configured = env.get("TDM_WEB_LOGIN_HEADLESS")
        headless = (not windows and not env.get("DISPLAY")) if configured is None else configured.strip().lower() in {"1", "true", "yes"}
        return [binary, *(["--headless=new"] if headless else []), "--no-first-run", "--no-default-browser-check", "--lang=zh-CN",
                "--window-size=1100,800", f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                "data:text/html,<p>正在打开 Twitch 登录页面…</p>"]

    async def start(self):
        binary = None
        if os.name == "nt":
            for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")):
                for vendor, app, exe in (("Google", "Chrome", "chrome.exe"), ("Microsoft", "Edge", "msedge.exe")):
                    candidate = Path(base) / vendor / app / "Application" / exe
                    if base and candidate.is_file():
                        binary = str(candidate)
                        break
                if binary:
                    break
        if not binary:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                binary = p.chromium.executable_path
        self.profile = Path(tempfile.mkdtemp(prefix="hdtool-twitch-"))
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        self.process = subprocess.Popen(self.launch_arguments(binary, self.profile, port),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.http = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=2))
        target = None
        for _ in range(50):
            if self.process.poll() is not None:
                raise RuntimeError("server browser exited")
            try:
                async with self.http.get(f"http://127.0.0.1:{port}/json/list") as response:
                    target = next((p for p in await response.json() if p["type"] == "page"), None)
                if target:
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(.2)
        if not target:
            raise RuntimeError("server browser did not start")
        self.ws = await self.http.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=4_000_000)
        await self.call("Page.enable")
        await self.call("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False})
        await self.call("Page.navigate", {"url": "https://www.twitch.tv/login"})

    async def call(self, method, params=None):
        async with self.lock:
            self.counter += 1
            request_id = self.counter
            await self.ws.send_json({"id": request_id, "method": method, "params": params or {}})
            while True:
                response = await self.ws.receive_json(timeout=10)
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError("browser command failed: " + method)
                    return response.get("result", {})

    async def screenshot(self):
        # Chrome may have no render surface immediately after Page.navigate.
        # Treat the initial capture error as a short paint race, not a failed login.
        for attempt in range(10):
            try:
                return (await self.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}))["data"]
            except RuntimeError:
                if attempt == 9:
                    raise
                await asyncio.sleep(.3)

    async def cookies(self):
        result = await self.call("Storage.getCookies")
        # These values never go to the web client or logs.
        return {c["name"]: c["value"] for c in result.get("cookies", [])
                if c["domain"].lstrip(".") in ("twitch.tv", "www.twitch.tv") and c["name"] in ("auth-token", "unique_id")}

    async def input(self, message):
        kind = message.get("type")
        if kind == "text":
            text = message.get("text")
            if not isinstance(text, str) or len(text) > 2000:
                raise ValueError("invalid text")
            await self.call("Input.insertText", {"text": text})
        elif kind in ("down", "up", "wheel"):
            x, y = message.get("x"), message.get("y")
            if type(x) not in (float, int) or type(y) not in (float, int) or not (0 <= x <= WIDTH and 0 <= y <= HEIGHT):
                raise ValueError("invalid pointer")
            params = {"x": x, "y": y, "type": {"down": "mousePressed", "up": "mouseReleased", "wheel": "mouseWheel"}[kind]}
            if kind == "wheel":
                delta = message.get("delta", 0)
                if type(delta) not in (float, int):
                    raise ValueError("invalid wheel")
                params.update(deltaX=0, deltaY=max(-800, min(800, delta)))
            else:
                params.update(button="left", clickCount=1)
            await self.call("Input.dispatchMouseEvent", params)
        elif kind == "key":
            key = message.get("key")
            select_all = key == "a" and message.get("select_all") is True
            if key not in ALLOWED_KEYS and not select_all:
                raise ValueError("unsupported key")
            code = 65 if select_all else ALLOWED_KEYS[key]
            params = {"key": "a" if select_all else key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "modifiers": 2 if select_all else 0}
            await self.call("Input.dispatchKeyEvent", {**params, "type": "rawKeyDown"})
            await self.call("Input.dispatchKeyEvent", {**params, "type": "keyUp"})
        else:
            raise ValueError("unsupported input")

    async def close(self):
        if self.ws and not self.ws.closed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.call("Browser.close"), 2)
            await self.ws.close()
        if self.http:
            await self.http.close()
        if self.process and self.process.poll() is None:
            try:
                await asyncio.to_thread(self.process.wait, timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(self.process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                else:
                    self.process.kill()
                await asyncio.to_thread(self.process.wait)
        if self.profile:
            resolved = self.profile.resolve()
            if resolved.parent == Path(tempfile.gettempdir()).resolve() and resolved.name.startswith("hdtool-twitch-"):
                shutil.rmtree(resolved, ignore_errors=True)


class RemoteLoginManager:
    def __init__(self, factory=RemoteBrowser):
        self.factory = factory
        self.sessions = {}
        self.lock = asyncio.Lock()

    async def open(self, user_id):
        async with self.lock:
            if user_id in self.sessions:
                raise RuntimeError("你已有一个网页登录窗口，请先关闭它")
            if len(self.sessions) >= 2:
                raise RuntimeError("网页登录正在被使用，请稍后重试（最多同时 2 人登录）")
            browser = self.factory()
            self.sessions[user_id] = browser
        try:
            await browser.start()
            return browser
        except BaseException:
            await self.close(user_id, browser)
            raise

    async def close(self, user_id, browser):
        try:
            await browser.close()
        finally:
            if self.sessions.get(user_id) is browser:
                self.sessions.pop(user_id, None)

    async def shutdown(self):
        for user_id, browser in list(self.sessions.items()):
            await self.close(user_id, browser)
