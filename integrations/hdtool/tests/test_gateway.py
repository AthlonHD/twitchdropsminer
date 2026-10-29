import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import tempfile
import unittest

import aiohttp
from aiohttp import web
import uvicorn

MODULE = Path(__file__).resolve().parents[1] / "gateway.py"
spec = importlib.util.spec_from_file_location("gateway", MODULE)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class FakeEngine:
    def __init__(self, store):
        self.store = store
        self.backends = {}
        self.settings = {}
        self.submitted = {}
        self.lock = asyncio.Lock()

    def running(self, user):
        return user["id"] in self.backends

    async def start(self, user):
        async with self.lock:
            if user["id"] in self.backends:
                return self.backends[user["id"]][1]
            if not user["enabled"] or not user["running"]:
                raise gateway.HTTPException(409, "stopped")
            secret = "secret-" + user["id"]

            async def handle(request):
                if request.headers.get("X-Fleet-Password") != secret:
                    return web.Response(status=401)
                if request.path == "/socket.io/" and request.headers.get("Upgrade", "").lower() == "websocket":
                    ws = web.WebSocketResponse()
                    await ws.prepare(request)
                    await ws.send_str(user["name"])
                    async for msg in ws:
                        await ws.send_str(user["name"] + ":" + msg.data)
                    return ws
                if request.path == "/":
                    return web.Response(text="<html><body>" + user["name"] + "</body></html>", content_type="text/html")
                if request.path == "/api/instance":
                    return web.json_response({"login": user["name"]})
                if request.path == "/api/channels":
                    return web.json_response({"channels": []})
                if request.path == "/api/stats":
                    return web.json_response({"by_day": []})
                if request.path == "/api/drops-history":
                    return web.json_response([])
                if request.path == "/api/settings":
                    if request.method == "POST":
                        self.settings[user["id"]] = await request.json()
                    return web.json_response({"games_to_watch": [], "proxy": ""})
                if request.path == "/api/login/browser/helper-session":
                    self.submitted[user["id"]] = await request.json()
                return web.json_response({"user_id": user["id"], "status": user["name"], "paused": False})

            app = web.Application()
            app.router.add_route("*", "/{path:.*}", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            backend = {"port": site._server.sockets[0].getsockname()[1], "password": secret}
            self.backends[user["id"]] = (runner, backend)
            return backend

    async def stop(self, user):
        entry = self.backends.pop(user["id"], None)
        if entry:
            await entry[0].cleanup()

    async def close(self):
        for user in list(self.store.users.values()):
            await self.stop(user)


class FakeRemoteBrowser:
    def __init__(self):
        self.done = False
        self.closed = False

    async def input(self, message):
        self.done = message.get("type") == "key"

    async def cookies(self):
        return {"auth-token": "not-a-real-twitch-token", "unique_id": "test-device"} if self.done else {}

    async def screenshot(self):
        import base64
        return base64.b64encode(b"frame").decode()


class FakeRemoteManager:
    def __init__(self):
        self.instances = {}

    async def open(self, user_id):
        browser = FakeRemoteBrowser()
        self.instances[user_id] = browser
        return browser

    async def close(self, user_id, browser):
        browser.closed = True

    async def shutdown(self):
        for browser in self.instances.values():
            browser.closed = True


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hdtool-gateway-test-")
        root = Path(self.temp.name)
        self.source = root / "source"
        (self.source / "data").mkdir(parents=True)
        (self.source / "scripts").mkdir()
        (self.source / "data/web_config.json").write_text(json.dumps({"password": "admin-test-password", "setup_done": True}))
        (self.source / "scripts/tdm_login_helper.py").write_text('''import getpass, json, urllib.request, urllib.error
PASSWORD_TRIES = 3
def ensure_password(base_url, password, prompt=getpass.getpass):
    return password
def submit_session(base_url, password, auth_token, unique_id):
    return {"X-Fleet-Password": password}
''', encoding="utf-8")
        self.app = gateway.create_app(root / "platform", self.source, FakeEngine, FakeRemoteManager)
        self.store = self.app.state.store
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error", access_log=False, ws="websockets"))
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.task.done():
                await self.task
            await asyncio.sleep(.01)
        self.clients = []
        self.admin = await self.client()
        await self.login(self.admin, "admin", "admin-test-password")

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.server.should_exit = True
        await self.task
        self.temp.cleanup()

    async def client(self):
        client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"Origin": self.base})
        self.clients.append(client)
        return client

    async def login(self, client, username, password=None, expected=200):
        body = {"username": username}
        if password is not None:
            body["password"] = password
        async with client.post(self.base + "/portal/api/login", json=body) as response:
            self.assertEqual(response.status, expected, await response.text())
            result = await response.json()
            if expected == 200:
                self.assertEqual(result["next"], "/portal" if username == "admin" else "/")
            return result

    async def create(self, name):
        async with self.admin.post(self.base + "/portal/api/users", json={"username": name}) as r:
            self.assertEqual(r.status, 200, await r.text())
            return await r.json()

    async def test_username_only_and_administrator_permissions(self):
        alice = await self.create("alice")
        client = await self.client()
        await self.login(client, "alice")
        for path in ("/portal", "/portal/login"):
            async with client.get(self.base + path, allow_redirects=False) as r:
                self.assertEqual(r.status, 302)
                self.assertEqual(r.headers["Location"], "/")
        async with client.get(self.base + "/") as r:
            html = await r.text()
            self.assertIn("退出登录", html)
            self.assertNotIn("HD Tool 用户中心", html)
        async with self.admin.get(self.base + "/portal", allow_redirects=False) as r:
            self.assertEqual(r.status, 200)
        async with client.get(self.base + "/portal/api/me") as r:
            self.assertEqual((await r.json())["id"], alice["id"])
        for path, method, body in [
            ("/portal/api/users", "GET", None),
            ("/portal/api/users", "POST", {"username": "hacker"}),
            ("/portal/api/users/" + alice["id"], "POST", {"enabled": False}),
            ("/portal/api/password", "POST", {"password": "new-test-password"}),
        ]:
            async with client.request(method, self.base + path, json=body) as r:
                self.assertEqual(r.status, 403, path)
        await self.login(await self.client(), "not-created", expected=401)
        await self.login(await self.client(), "admin", expected=401)
        for user in self.store.users.values():
            self.assertNotIn("password", user)
        self.assertNotIn("hash", self.store.users[alice["id"]])
        self.assertNotIn("admin-test-password", self.store.file.read_text())

    async def test_http_fleet_scope_and_legacy_8080(self):
        alice, bob = await self.create("alice"), await self.create("bob")
        ca, cb = await self.client(), await self.client()
        await self.login(ca, "alice")
        await self.login(cb, "bob")
        for client, user in ((ca, alice), (cb, bob)):
            async with client.get(self.base + "/api/status", headers={"X-Fleet-Password": "forged", "X-User-Id": bob["id"]}) as r:
                self.assertEqual((await r.json())["user_id"], user["id"])
            async with client.get(self.base + "/api/accounts/overview") as r:
                data = await r.json()
                self.assertEqual([a["label"] for a in data["accounts"]], [user["name"]])
                self.assertNotIn("8080", json.dumps(data))
                self.assertTrue(data["accounts"][0]["reachable"])
            async with client.get(self.base + "/api/instances") as r:
                self.assertEqual((await r.json())["instances"], [])
        async with self.admin.get(self.base + "/api/accounts/overview") as r:
            self.assertEqual(len((await r.json())["accounts"]), 3)
        bob_n = list(self.store.users).index(bob["id"]) + 1
        for path, body in [("/api/accounts/bulk-action", {"targets": [bob_n], "action": "pause"}), ("/api/accounts/bulk-settings", {"targets": [bob_n], "field": "games_to_watch", "values": []})]:
            async with ca.post(self.base + path, json=body) as r:
                self.assertEqual(r.status, 403)
        for path in ("/api/instances/register", "/api/accounts/switch", "/api/self-update", "/api/pair/generate", "/api/auth/disable-password", "/api/settings/verify-proxy"):
            async with ca.post(self.base + path, json={"label": "../bob", "port": 8765}) as r:
                self.assertEqual(r.status, 403, path)
        async with ca.post(self.base + "/api/settings", json={"proxy": "http://127.0.0.1:8765"}) as r:
            self.assertEqual(r.status, 403)
        async with ca.post(self.base + "/api/settings", json={"proxy": "", "games_to_watch": ["My Game"], "language": "English"}) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.app.state.engine.settings[alice["id"]]["language"], "简体中文")
        async with ca.get(self.base + "/api/languages") as r:
            self.assertEqual(await r.json(), {"available": ["简体中文"], "current": "简体中文"})

    async def test_websocket_scope_revocation_and_cross_origin(self):
        alice = await self.create("alice")
        client = await self.client()
        await self.login(client, "alice")
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await client.ws_connect(self.base + "/socket.io/?EIO=4&transport=websocket", headers={"Origin": "https://evil.example"})
        async with client.ws_connect(self.base + "/socket.io/?EIO=4&transport=websocket") as ws:
            self.assertEqual((await ws.receive()).data, "alice")
            await ws.send_str("ping")
            self.assertEqual((await ws.receive()).data, "alice:ping")
            async with self.admin.post(self.base + "/portal/api/users/" + alice["id"], json={"enabled": False}) as r:
                self.assertEqual(r.status, 200)
            msg = await asyncio.wait_for(ws.receive(), 3)
            self.assertIn(msg.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING))
        async with client.get(self.base + "/api/status") as r:
            self.assertEqual(r.status, 401)
        await self.login(await self.client(), "alice", expected=401)

    async def test_csrf_stop_restart_helper_and_no_registration(self):
        alice = await self.create("alice")
        client = await self.client()
        await self.login(client, "alice")
        async with client.post(self.base + "/portal/api/instance", json={"action": "stop"}, headers={"Origin": "https://evil.example"}) as r:
            self.assertEqual(r.status, 403)
        async with client.post(self.base + "/portal/api/instance", json={"action": "stop"}) as r:
            self.assertEqual(r.status, 200)
        self.assertFalse(self.app.state.engine.running(self.store.users[alice["id"]]))
        async with client.get(self.base + "/api/status") as r:
            self.assertEqual(r.status, 409)
        async with client.post(self.base + "/portal/api/instance", json={"action": "start"}) as r:
            self.assertEqual(r.status, 200)
        async with client.get(self.base + "/login-helper.py") as r:
            helper = await r.text()
            compile(helper, "login-helper.py", "exec")
            self.assertIn('"Bearer " + password', helper)
            self.assertIn('username.lower() == "admin" else ""', helper)
        helper_client = await self.client()
        async with helper_client.post(self.base + "/portal/api/login", json={"username": "alice", "helper": True}, headers={"Origin": self.base}) as r:
            token = (await r.json())["token"]
        anonymous = await self.client()
        async with anonymous.post(self.base + "/api/login/browser/helper-session", json={"auth_token": "test-only"}, headers={"Authorization": "Bearer " + token}) as r:
            self.assertEqual((await r.json())["user_id"], alice["id"])
        async with anonymous.post(self.base + "/portal/api/register", json={"username": "intruder"}) as r:
            self.assertEqual(r.status, 401)

    async def test_no_install_login_is_authenticated_and_submits_only_to_owner(self):
        alice = await self.create("alice")
        bob = await self.create("bob")
        client = await self.client()
        await self.login(client, "alice")
        async with client.get(self.base + "/portal/twitch-login") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("无需在你的设备安装软件", await r.text())
        anonymous = await self.client()
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await anonymous.ws_connect(self.base + "/portal/twitch-login/ws")
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await client.ws_connect(self.base + "/portal/twitch-login/ws", headers={"Origin": "https://evil.example"})
        async with client.ws_connect(self.base + "/portal/twitch-login/ws") as ws:
            first = await ws.receive_json()
            self.assertEqual(first["type"], "status")
            frame = await ws.receive()
            self.assertEqual(frame.type, aiohttp.WSMsgType.BINARY)
            await ws.send_json({"type": "key", "key": "Enter"})
            for _ in range(10):
                message = await ws.receive(timeout=3)
                if message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data).get("type") == "done":
                    break
            else:
                self.fail("remote login did not complete")
        submitted = self.app.state.engine.submitted
        self.assertIn(alice["id"], submitted)
        self.assertNotIn(bob["id"], submitted)


if __name__ == "__main__":
    unittest.main()
