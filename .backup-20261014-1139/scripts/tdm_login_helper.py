#!/usr/bin/env python3
"""Run this on your own device (not the server) to log in to Twitch for TDM.

Why this exists: Twitch's own bot/integrity check rejects TDM's embedded
server-side login (the noVNC panel in the dashboard) with "Your browser is
not currently supported" -- it detects the Playwright-driven browser, and
datacenter IPs make it worse. This script opens your own, already-installed
Google Chrome (or Edge) as an ordinary browser window, NOT driven by
Playwright, on your own device and network, and sends the resulting
session to your TDM instance automatically once you've logged in. It only
reads the browser's cookie store; it never attaches to or scripts the page.

Requirements: Python 3.10+, Google Chrome or Microsoft Edge installed, and
the `aiohttp` package (`pip install aiohttp`).

Usage:
    python tdm_login_helper.py --url http://localhost:8080
    python tdm_login_helper.py --url https://your-tdm-domain.example --password yourpassword

--url is your TDM instance's address (wherever its dashboard is reachable
from this device -- same host or a remote one, doesn't matter). --password
is your TDM dashboard password, if you set one (see WEB_PASSWORD in TDM's
own README); omit it if you didn't set one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile

try:
    import aiohttp
except ImportError:
    print("Missing dependency: pip install aiohttp", file=sys.stderr)
    sys.exit(1)

LOGIN_URL = "https://www.twitch.tv/login"
COOKIE_DOMAIN = "twitch.tv"
COOKIE_NAME = "auth-token"
DEVICE_ID_COOKIE_NAME = "unique_id"
LOGIN_TIMEOUT_SEC = 600  # 10 minutes to complete login, same as TDM's own
POLL_INTERVAL_SEC = 1.5
SUBMIT_RETRY_SEC = 90  # TDM re-offers a timed-out login attempt after ~20s


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


# Tried in order. All are launched as plain processes -- an ordinary,
# non-automated browser is the whole point.
BROWSERS = ("chrome", "edge", "chromium")


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


async def wait_for_login(port: int) -> dict[str, str]:
    """Poll the browser's cookie store (browser-level CDP only, no page
    attached) until Twitch's auth-token cookie appears."""
    async with aiohttp.ClientSession() as http:
        ws_url = None
        for _ in range(60):
            try:
                async with http.get(f"http://127.0.0.1:{port}/json/version") as resp:
                    ws_url = (await resp.json())["webSocketDebuggerUrl"]
                    break
            except aiohttp.ClientError:
                await asyncio.sleep(0.5)
        if ws_url is None:
            raise RuntimeError("The browser did not open its local debug port")
        print("Log in to Twitch in the browser window that just opened and keep it open -- "
              "it closes by itself once the login was sent to TDM. Waiting...", flush=True)
        async with http.ws_connect(ws_url, max_msg_size=0) as ws:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + LOGIN_TIMEOUT_SEC
            msg_id = 0
            while loop.time() < deadline:
                msg_id += 1
                await ws.send_json({"id": msg_id, "method": "Storage.getCookies"})
                while (reply := await ws.receive_json(timeout=30)).get("id") != msg_id:
                    pass
                found = {
                    c["name"]: c["value"]
                    for c in reply.get("result", {}).get("cookies", [])
                    if c["domain"].endswith(COOKIE_DOMAIN)
                }
                if found.get(COOKIE_NAME):
                    return {"auth_token": found[COOKIE_NAME], "unique_id": found.get(DEVICE_ID_COOKIE_NAME, "")}
                await asyncio.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"No login detected within {LOGIN_TIMEOUT_SEC}s")


async def submit_session(base_url: str, password: str | None, auth_token: str, unique_id: str) -> None:
    headers = {"X-Fleet-Password": password} if password else {}
    url = base_url.rstrip("/") + "/api/login/browser/helper-session"
    async with aiohttp.ClientSession() as http:
        async with http.post(
            url, json={"auth_token": auth_token, "unique_id": unique_id}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
        ) as response:
            if response.status == 409:
                raise NoLoginInProgress(
                    "TDM has no login attempt in progress -- open its dashboard first "
                    "(it starts one automatically whenever a login is needed) and try again."
                )
            if response.status == 401:
                raise RuntimeError("TDM rejected the request -- check --password matches its dashboard password.")
            if 300 <= response.status < 400:
                raise RuntimeError(
                    "TDM redirected the request -- finish its first-run dashboard setup (open the "
                    "dashboard once in a browser) and pass the password you set there via --password."
                )
            response.raise_for_status()


class NoLoginInProgress(RuntimeError):
    pass


async def submit_with_retry(base_url: str, password: str | None, auth_token: str, unique_id: str) -> None:
    """TDM restarts a timed-out login attempt after a short pause; a login
    that lands in that gap is retried instead of thrown away."""
    deadline = asyncio.get_running_loop().time() + SUBMIT_RETRY_SEC
    while True:
        try:
            return await submit_session(base_url, password, auth_token, unique_id)
        except NoLoginInProgress:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            print("TDM has no login attempt in progress right now, retrying...", flush=True)
            await asyncio.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Your TDM instance's address, e.g. http://localhost:8080")
    parser.add_argument("--password", default=None, help="TDM dashboard password, if one is set")
    parser.add_argument(
        "--browser", choices=BROWSERS, default=None,
        help="Force a specific browser instead of trying chrome, then edge, then chromium",
    )
    args = parser.parse_args()

    binary = find_browser(args.browser)
    if binary is None:
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
            cookies = asyncio.run(wait_for_login(port))
        except aiohttp.ClientConnectionResetError:
            print("Login not completed: the browser window was closed before a login was detected.",
                  file=sys.stderr)
            return 1
        except (TimeoutError, RuntimeError, aiohttp.ClientError) as exc:
            print(f"Login not completed: {exc}", file=sys.stderr)
            return 1

        print("Login detected, sending the session to TDM...")
        try:
            asyncio.run(submit_with_retry(args.url, args.password, cookies["auth_token"], cookies["unique_id"]))
        except Exception as exc:
            print(f"Failed to send the session to TDM: {exc}", file=sys.stderr)
            return 1

        print("Done -- TDM should now show you as logged in.")
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
