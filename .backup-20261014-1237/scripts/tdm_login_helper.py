#!/usr/bin/env python3
"""Log in to Twitch for a TwitchDropsMiner (TDM) instance that runs on a server.

Run it on your own PC (Python 3 + Google Chrome or Microsoft Edge, nothing
else to install). It opens a normal browser window on the Twitch login
page; once you're logged in it sends the session to your TDM and closes.

    python tdm_login_helper.py https://your-tdm.example

Your TDM dashboard shows this command with its own address already filled
in. If the dashboard has a password, you're asked for it.

Why: Twitch rejects remote-controlled browsers, so the login has to happen
in an ordinary browser on your own device. This script only reads the
browser's cookie store; it never touches the page itself.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

LOGIN_URL = "https://www.twitch.tv/login"
COOKIE_DOMAIN = "twitch.tv"
COOKIE_NAME = "auth-token"
DEVICE_ID_COOKIE_NAME = "unique_id"
LOGIN_TIMEOUT_SEC = 600
POLL_INTERVAL_SEC = 1.5
SUBMIT_RETRY_SEC = 90  # TDM re-offers a timed-out login attempt after ~20s
PASSWORD_TRIES = 3
BROWSERS = ("chrome", "edge", "chromium")


class NoLoginInProgress(RuntimeError):
    pass


class PasswordRequired(RuntimeError):
    pass


class BrowserClosed(RuntimeError):
    pass


def _candidates(name: str) -> list[str]:
    """Usual install locations (plus PATH names) for one browser."""
    pf = [os.environ.get(v, "") for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    win = {
        "chrome": [os.path.join(b, "Google", "Chrome", "Application", "chrome.exe") for b in pf if b],
        "edge": [os.path.join(b, "Microsoft", "Edge", "Application", "msedge.exe") for b in pf if b],
        "chromium": [],
    }
    mac = {
        "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
    }
    path_names = {
        "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
        "edge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
        "chromium": ["chromium", "chromium-browser"],
    }
    found = [p for p in win[name] + mac[name] if os.path.exists(p)]
    found += [p for p in (shutil.which(n) for n in path_names[name]) if p]
    return found


def find_browser(preferred: str | None) -> str | None:
    for name in [preferred] if preferred else BROWSERS:
        paths = _candidates(name)
        if paths:
            return paths[0]
    print(
        "Could not find a browser. Install Google Chrome or Microsoft Edge on this device"
        + (f" (or drop --browser {preferred})." if preferred else "."),
        file=sys.stderr,
    )
    return None


def ws_frame(payload: bytes) -> bytes:
    """A single masked text frame, as a WebSocket client must send it."""
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 1 << 16:
        header.append(0x80 | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += n.to_bytes(8, "big")
    mask = os.urandom(4)
    return bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class CdpSocket:
    """Just enough WebSocket client for Chrome's local DevTools port."""

    def __init__(self, ws_url: str) -> None:
        url = urllib.parse.urlparse(ws_url)
        self._sock = socket.create_connection((url.hostname, url.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (
                f"GET {url.path} HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise BrowserClosed("browser closed during the handshake")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("the browser refused the DevTools connection")
        self._buffer = response.split(b"\r\n\r\n", 1)[1]
        self._next_id = 0

    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            try:
                chunk = self._sock.recv(65536)
            except ConnectionError:
                chunk = b""  # Windows resets instead of closing cleanly
            if not chunk:
                raise BrowserClosed("the browser window was closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _receive(self) -> dict:
        message = b""
        while True:
            first, second = self._read(2)
            n = second & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            payload = self._read(n)
            opcode = first & 0x0F
            if opcode == 0x8:
                raise BrowserClosed("the browser window was closed")
            if opcode in (0x0, 0x1):
                message += payload
                if first & 0x80:
                    return json.loads(message)

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        try:
            self._sock.sendall(ws_frame(json.dumps({"id": self._next_id, "method": method, "params": params or {}}).encode()))
        except ConnectionError:
            raise BrowserClosed("the browser window was closed") from None
        while True:
            reply = self._receive()
            if reply.get("id") == self._next_id:
                return reply

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _devtools_url(port: int) -> str:
    deadline = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                return json.load(resp)["webSocketDebuggerUrl"]
        except (OSError, ValueError):
            if time.monotonic() > deadline:
                raise RuntimeError("the browser did not open its local DevTools port")
            time.sleep(0.5)


def wait_for_login(port: int) -> dict[str, str]:
    cdp = CdpSocket(_devtools_url(port))
    print("Log in to Twitch in the browser window that just opened and keep it open -- "
          "it closes by itself once the login was sent to TDM. Waiting...", flush=True)
    try:
        deadline = time.monotonic() + LOGIN_TIMEOUT_SEC
        while time.monotonic() < deadline:
            cookies = cdp.call("Storage.getCookies").get("result", {}).get("cookies", [])
            found = {c["name"]: c["value"] for c in cookies if c["domain"].endswith(COOKIE_DOMAIN)}
            if found.get(COOKIE_NAME):
                return {"auth_token": found[COOKIE_NAME], "unique_id": found.get(DEVICE_ID_COOKIE_NAME, "")}
            time.sleep(POLL_INTERVAL_SEC)
        raise TimeoutError(f"no login detected within {LOGIN_TIMEOUT_SEC // 60} minutes")
    finally:
        cdp.close()


def check_password(base_url: str, password: str | None) -> None:
    """Ask TDM whether the dashboard password is right before anyone logs in
    to Twitch: an empty session gets 400 past the password check, 401 if
    the password is wrong. Raises PasswordRequired on 401."""
    try:
        submit_session(base_url, password, "", "")
    except RuntimeError as exc:
        if isinstance(exc, PasswordRequired):
            raise
        if "HTTP 400" not in str(exc):
            raise


def ensure_password(base_url: str, password: str | None, prompt=getpass.getpass) -> str | None:
    for attempt in range(PASSWORD_TRIES + 1):
        try:
            check_password(base_url, password)
            return password
        except PasswordRequired:
            if attempt == PASSWORD_TRIES:
                break
            if password is not None:
                print("Wrong password.", file=sys.stderr)
            password = prompt("TDM dashboard password: ")
    raise RuntimeError("TDM rejected the dashboard password.")


def submit_session(base_url: str, password: str | None, auth_token: str, unique_id: str) -> None:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/login/browser/helper-session",
        data=json.dumps({"auth_token": auth_token, "unique_id": unique_id}).encode(),
        headers={"Content-Type": "application/json", **({"X-Fleet-Password": password} if password else {})},
        method="POST",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(request, timeout=15).close()
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise NoLoginInProgress("TDM has no login attempt in progress -- open its dashboard and try again.")
        if exc.code == 401:
            raise PasswordRequired()
        if 300 <= exc.code < 400:
            raise RuntimeError("TDM redirected the request -- open its dashboard once in a browser to finish its first-run setup.")
        raise RuntimeError(f"TDM answered HTTP {exc.code}")


def submit_with_retry(base_url: str, password: str | None, auth_token: str, unique_id: str) -> None:
    deadline = time.monotonic() + SUBMIT_RETRY_SEC
    while True:
        try:
            return submit_session(base_url, password, auth_token, unique_id)
        except NoLoginInProgress:
            if time.monotonic() >= deadline:
                raise
            print("TDM has no login attempt in progress right now, retrying...", flush=True)
            time.sleep(5)


def send_with_password_prompt(base_url, password, auth_token, unique_id, prompt=getpass.getpass) -> None:
    for attempt in range(PASSWORD_TRIES + 1):
        try:
            return submit_with_retry(base_url, password, auth_token, unique_id)
        except PasswordRequired:
            if attempt == PASSWORD_TRIES:
                break
            if password is not None:
                print("Wrong password.", file=sys.stderr)
            password = prompt("TDM dashboard password: ")
    raise RuntimeError("TDM rejected the dashboard password.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Log in to Twitch for a TDM instance.")
    parser.add_argument("url", nargs="?", help="your TDM address, e.g. https://tdm.example or http://192.168.1.10:8080")
    parser.add_argument("--url", dest="url_option", help=argparse.SUPPRESS)
    parser.add_argument("--password", default=None, help="TDM dashboard password (asked for if needed)")
    parser.add_argument("--browser", choices=BROWSERS, default=None, help="force chrome, edge or chromium")
    args = parser.parse_args(argv)
    url = args.url or args.url_option
    if not url:
        parser.error("give your TDM address, e.g. python tdm_login_helper.py https://tdm.example")

    binary = find_browser(args.browser)
    if binary is None:
        return 1
    try:
        password = ensure_password(url, args.password)
    except Exception as exc:
        print(f"Can't reach TDM: {exc}", file=sys.stderr)
        return 1
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile_dir = tempfile.mkdtemp(prefix="tdm-login-")
    proc = subprocess.Popen([
        binary, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check", LOGIN_URL,
    ])
    try:
        try:
            cookies = wait_for_login(port)
        except BrowserClosed:
            print("Login not completed: the browser window was closed before a login was detected.", file=sys.stderr)
            return 1
        except (TimeoutError, RuntimeError, OSError) as exc:
            print(f"Login not completed: {exc}", file=sys.stderr)
            return 1
        print("Login detected, sending the session to TDM...", flush=True)
        try:
            send_with_password_prompt(url, password, cookies["auth_token"], cookies["unique_id"])
        except Exception as exc:
            print(f"Failed to send the session to TDM: {exc}", file=sys.stderr)
            return 1
        print("Done -- TDM is logged in now.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
