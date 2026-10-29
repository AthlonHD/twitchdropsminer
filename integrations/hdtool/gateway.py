"""HD Tool's administrator-provisioned, isolated TDM web gateway."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import aiohttp
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect

# Direct execution by Agent and the isolated tests use the fork's own module.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integrations.hdtool.remote_login import RemoteLoginManager

LOG = logging.getLogger("hdtool.tdm")
COOKIE = "hdtool_tdm_session"
SESSION_SECONDS = 12 * 60 * 60
ITERATIONS = 600_000
SAFE_SETTINGS = set("connection_quality dark_mode games_to_watch language inventory_list_view allow_unknown_link inventory_filters minimum_refresh_interval_minutes mining_benefits claim_channel_points idle_channels idle_parallel idle_use_followed preferred_games priority_list_only scheduler_enabled scheduler_start scheduler_stop drop_name_blacklist blacklisted_drop_ids ignored_campaign_ids auto_prioritize auto_add_linked auto_add_excluded_games tab_counter_enabled claim_moments irc_chat_presence irc_mention_notify make_predictions bet_strategy bet_percentage bet_max_points bet_minimum_points bet_percentage_gap bet_delay_seconds prediction_channels channel_strategies".split())
READ_PATHS = set("/ /favicon.ico /logo.png /manifest.json /api/status /api/health /api/channels /api/campaigns /api/console /api/analytics/subscribed-channels /api/analytics/followed-channels /api/analytics/points /api/drops-history /api/drops-history/export.csv /api/stats /api/idle-watch/status /api/settings /api/languages /api/translations /api/instance /api/version /api/wanted-items /api/predictions /api/streamer-overrides /api/auth/status".split())
WRITE_PATHS = set("/api/pause /api/resume /api/channels/select /api/idle-watch/switch /api/idle-watch/resume /api/settings /api/login/browser/cancel /api/login/browser/helper-session /api/reload /api/skip-game /api/mode/exit-manual /api/streamer-overrides".split())


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def set_password(user: dict, password: str) -> None:
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise HTTPException(400, "密码须为 10–128 个字符")
    user["salt"] = secrets.token_hex(16)
    user["hash"] = password_hash(password, user["salt"])
    user["version"] = user.get("version", 0) + 1


class Store:
    def __init__(self, root: Path, source: Path):
        self.root, self.source = root.resolve(), source.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "users.json"
        self.sessions: dict[str, dict] = {}
        self.failures: dict[str, list[float]] = {}
        self.users: dict[str, dict] = json.loads(self.file.read_text("utf-8")) if self.file.exists() else {}
        if not self.users:
            old_config = source / "data" / "web_config.json"
            old = json.loads(old_config.read_text("utf-8-sig")) if old_config.exists() else {}
            password = old.get("password") or secrets.token_urlsafe(24)
            admin = self.create("admin", password, admin=True)
            old_data = source / "data"
            dest = self.data(admin)
            if old_data.exists():
                shutil.copytree(old_data, dest, dirs_exist_ok=True)
            if not old.get("password"):
                (self.root / "initial-admin-password.txt").write_text(password, encoding="utf-8")
            LOG.warning("[TDM] 平台管理员：admin；沿用原 Dashboard 密码（新部署密码见 initial-admin-password.txt）。")

    def save(self):
        temp = self.file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.file)

    def create(self, name: str, password: str | None = None, admin=False) -> dict:
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", name):
            raise HTTPException(400, "用户名须为 3–32 位小写字母、数字、下划线或短横线")
        if any(u["name"] == name for u in self.users.values()):
            raise HTTPException(409, "用户名已存在")
        if len(self.users) >= 32:
            raise HTTPException(400, "当前最多支持 32 个平台用户")
        user = {"id": uuid.uuid4().hex, "name": name, "admin": admin, "enabled": True, "running": True}
        if admin:
            set_password(user, password)
        else:
            user["version"] = 1
        self.users[user["id"]] = user
        self.save()
        return user

    def data(self, user: dict) -> Path:
        return self.root / "instances" / user["id"]

    async def login(self, name: str, password: str, remote: str) -> str:
        now = time.time()
        name = name.lower().strip()[:128]
        keys = ("ip:" + remote, "user:" + name)
        self.failures = {k: [t for t in v if now - t < 300] for k, v in self.failures.items() if any(now - t < 300 for t in v)}
        if any(len(self.failures.get(k, [])) >= 10 for k in keys):
            raise HTTPException(429, "尝试过于频繁，请 5 分钟后再试")
        for k in keys:
            self.failures.setdefault(k, []).append(now)
        user = next((u for u in self.users.values() if u["name"] == name), None)
        salt = user.get("salt", "00" * 16) if user else "00" * 16
        candidate = await asyncio.to_thread(password_hash, password[:128], salt)
        if not user or not user["enabled"] or (user["admin"] and not hmac.compare_digest(candidate, user["hash"])):
            raise HTTPException(401, "用户名或密码错误")
        for k in keys:
            self.failures.pop(k, None)
        self.sessions = {k: v for k, v in self.sessions.items() if v["expiry"] > now}
        if len(self.sessions) >= 1024:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"id": user["id"], "version": user["version"], "expiry": now + SESSION_SECONDS}
        return token

    def user(self, token: str) -> dict | None:
        session = self.sessions.get(token)
        if not session or session["expiry"] <= time.time():
            return None
        user = self.users.get(session["id"])
        return user if user and user["enabled"] and user["version"] == session["version"] else None


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.processes: dict[str, tuple] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, user):
        return self.locks.setdefault(user["id"], asyncio.Lock())

    def running(self, user):
        p = self.processes.get(user["id"])
        return bool(p and p[0].poll() is None)

    async def start(self, user: dict) -> dict:
        async with self.lock(user):
            return await self._start(user)

    async def _start(self, user: dict) -> dict:
        existing = self.processes.get(user["id"])
        if existing and existing[0].poll() is None:
            return {"port": existing[1], "password": existing[2]}
        if existing:
            existing[3].close()
            self.processes.pop(user["id"])
        if not user["enabled"] or not user["running"]:
            raise HTTPException(409, "你的实例已停止，请到用户中心启动")
        data = self.store.data(user)
        data.mkdir(parents=True, exist_ok=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        secret = secrets.token_urlsafe(32)
        config = data / "web_config.json"
        cfg = json.loads(config.read_text("utf-8-sig")) if config.exists() else {}
        cfg.update(setup_done=True, password=secret)
        config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        settings_dir = data / "accounts" / cfg["active_account"] if cfg.get("active_account") else data
        if not settings_dir.resolve().is_relative_to(data.resolve()):
            raise RuntimeError("账号数据目录无效")
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_file = settings_dir / "settings.json"
        settings = json.loads(settings_file.read_text("utf-8-sig")) if settings_file.exists() else {}
        settings["language"] = "简体中文"
        settings_file.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        # Never let the upstream fleet feature discover another user's internal port.
        (data / "instances.json").write_text('{"instances": []}', encoding="utf-8")
        env = {**os.environ, "TDM_HOST": "127.0.0.1", "TDM_PORT": str(port), "TDM_REMOTE_LOGIN": "1", "TDM_DATA_DIR": str(data), "PYTHONUTF8": "1"}
        output = (data / "runner.log").open("ab")
        proc = subprocess.Popen([sys.executable, "-u", str(self.store.source / "main.py")], cwd=self.store.source,
                                env=env, stdout=output, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                start_new_session=os.name != "nt")
        self.processes[user["id"]] = (proc, port, secret, output)
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                for _ in range(30):
                    if proc.poll() is not None:
                        raise RuntimeError("TDM 进程提前退出")
                    try:
                        async with client.get(f"http://127.0.0.1:{port}/healthz", timeout=aiohttp.ClientTimeout(total=1)) as response:
                            if response.status == 200:
                                LOG.warning("[TDM] 用户 %s 实例已启动（仅本机监听）", user["name"])
                                return {"port": port, "password": secret}
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(.5)
            raise RuntimeError("TDM 健康检查超时")
        except BaseException:
            await self._stop(user)
            raise

    async def stop(self, user):
        async with self.lock(user):
            await self._stop(user)

    async def _stop(self, user):
        info = self.processes.pop(user["id"], None)
        if not info:
            return
        proc, _, _, output = info
        try:
            if proc.poll() is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.to_thread(proc.wait, timeout=10)
        finally:
            output.close()

    async def close(self):
        for user in list(self.store.users.values()):
            await self.stop(user)


STYLE = """body{font-family:system-ui,sans-serif;background:#11141c;color:#edf0f8;margin:0}main{max-width:960px;margin:48px auto;padding:24px}h1{font-size:28px}a{color:#baa6ff}section{padding:24px;background:#1c2230;border:1px solid #353e50;border-radius:14px;margin:20px 0}input,button{font:inherit;padding:11px;margin:5px;border-radius:8px;border:1px solid #4c576c}input{background:#10151e;color:white}button,.button{background:#7750cc;color:white;cursor:pointer;text-decoration:none;display:inline-block;padding:11px;border-radius:8px}small,p{color:#b6bfd0;line-height:1.7}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #353e50}#message{white-space:pre-wrap;color:#e9c079}.top{display:flex;gap:15px;align-items:center;flex-wrap:wrap}form{display:flex;flex-wrap:wrap;gap:4px}iframe{border:0}button.secondary{background:#354159}"""


def document(body, script=""):
    return f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HD Tool · Twitch 掉宝</title><style>{STYLE}</style><main>{body}</main><script>{script}</script></html>'


def render_dashboard(content: str, user: dict) -> str:
    """Keep fork-only controls in the upstream header's normal flex layout."""
    control = '<a class="btn-secondary" href="/portal" style="padding:7px 12px;white-space:nowrap;text-decoration:none">HD Tool 用户中心</a>'
    if not user["admin"]:
        control = '<button class="btn-secondary" type="button" onclick="fetch(\'/portal/api/logout\',{method:\'POST\'}).then(()=>location.href=\'/portal/login\')" style="padding:7px 12px;white-space:nowrap">退出登录</button>'
    # app.js still reads this element when saving settings; keep it hidden, with no switch UI.
    replacement = '<select id="language" hidden disabled style="display:none" aria-hidden="true"><option value="简体中文">简体中文</option></select>' + control
    content, count = re.subn(r'<label\s+for="language"\s+class="language-selector">.*?</label>', lambda _: replacement, content, count=1, flags=re.S)
    if not count:
        content = content.replace("<body>", '<body><nav style="display:flex;justify-content:flex-end;padding:12px">' + control + '</nav>', 1)
    style = '<style>#instances-card,#account-tabs{display:none!important}.header-brand-right{gap:10px;flex-wrap:wrap}</style>'
    web_login = '<div style="margin:12px 0"><a class="btn-primary" href="/portal/twitch-login" style="display:inline-block;padding:10px 16px;text-decoration:none">网页登录 Twitch（免安装 · 试用）</a><p style="margin-top:8px">直接在网页操作服务器浏览器，无需安装 Python。服务器登录可能需要额外验证；遇到不支持的浏览器或验证问题时，可使用下面的本机助手。</p></div>'
    content = content.replace('<p id="login-helper-intro">', web_login + '<p id="login-helper-intro">', 1)
    return content.replace("</head>", style + "</head>", 1)


LOGIN_PAGE = document("""<h1>HD Tool · Twitch 掉宝</h1><section><h2>登录用户中心</h2><p>请填写管理员为你创建的用户名。登录后绑定你自己的 Twitch。</p><form id="login"><input name="username" id="username" placeholder="用户名" autocomplete="username" required maxlength="32"><input name="password" id="admin-password" type="password" placeholder="管理员密码" autocomplete="current-password" maxlength="128" hidden><button>登录</button></form><p id="message"></p><small>普通用户无需密码；不开放注册。知道用户名的人均可进入对应空间，请勿把用户名当成公开昵称。</small></section>""", """document.querySelector('#username').oninput=e=>{document.querySelector('#admin-password').hidden=e.target.value.trim().toLowerCase()!=='admin'};document.querySelector('#login').onsubmit=async e=>{e.preventDefault();let r=await fetch('/portal/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});let result=await r.json();if(r.ok)location.href=result.next;else document.querySelector('#message').textContent=result.detail||'登录失败'};""")


HOME_PAGE = document("""<div class="top"><h1>Twitch 掉宝</h1><span id="who"></span><a href="/">打开我的 Dashboard</a><button id="logout" class="secondary">退出登录</button></div><p id="message"></p><section><h2>我的实例</h2><p id="state">正在查询…</p><button data-action="start">启动</button><button class="secondary" data-action="stop">停止</button><button class="secondary" data-action="restart">重启</button><p>每个人独立运行。关闭浏览器不会停止挂机。首次使用请进入 Dashboard，按登录面板提示在自己的电脑运行登录助手；普通用户在助手中只需填写平台用户名；管理员仍需密码。</p></section><section id="password-card" hidden><h2>修改管理员密码</h2><form id="password"><input name="old_password" type="password" placeholder="当前密码" autocomplete="current-password" required><input name="password" type="password" placeholder="新密码（至少 10 位）" autocomplete="new-password" minlength="10" maxlength="128" required><button>修改密码</button></form></section><section id="admin" hidden><h2>用户管理</h2><p>创建后将用户名和本站地址交给对方，无需密码。知道用户名的人均可进入对应空间，请为每人分配不公开的专属用户名。停用会停止该用户挂机并立即撤销登录。</p><form id="create"><input name="username" placeholder="用户名（字母、数字、_、-）" required maxlength="32"><button>创建用户</button></form><table><thead><tr><th>用户</th><th>状态</th><th>操作</th></tr></thead><tbody id="users"></tbody></table></section>""", """
const msg=t=>document.querySelector('#message').textContent=t;
async function api(path,data){let r=await fetch('/portal/api/'+path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});let b=await r.json();if(!r.ok){if(r.status===401)location.href='/portal/login';throw Error(b.detail||'操作失败')}return b}
async function refresh(){let me=await api('me');document.querySelector('#who').textContent=me.name+(me.admin?' · 管理员':'');document.querySelector('#state').textContent=me.running?'运行中':'已停止';document.querySelector('#admin').hidden=!me.admin;document.querySelector('#password-card').hidden=!me.admin;if(me.admin){let rows=await api('users');let list=document.querySelector('#users');list.replaceChildren();for(let u of rows){let row=document.createElement('tr');for(let text of [u.name+(u.admin?'（管理员）':''),u.enabled?(u.running?'运行中':'已停止'):'已停用']){let cell=document.createElement('td');cell.textContent=text;row.append(cell)}let actions=document.createElement('td');if(!u.admin){let toggle=document.createElement('button');toggle.textContent=u.enabled?'停用':'启用';toggle.onclick=async()=>{try{await api('users/'+u.id,{enabled:!u.enabled});await refresh()}catch(e){msg(e.message)}};actions.append(toggle);}row.append(actions);list.append(row)}}}
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api('instance',{action:b.dataset.action});await refresh();msg('操作完成')}catch(e){msg(e.message)}finally{b.disabled=false}});
document.querySelector('#create').onsubmit=async e=>{e.preventDefault();try{await api('users',Object.fromEntries(new FormData(e.target)));e.target.reset();await refresh();msg('用户已创建，对方输入用户名即可登录')}catch(e){msg(e.message)}};
document.querySelector('#password').onsubmit=async e=>{e.preventDefault();try{await api('password',Object.fromEntries(new FormData(e.target)));location.href='/portal/login'}catch(e){msg(e.message)}};
document.querySelector('#logout').onclick=async()=>{await api('logout',{});location.href='/portal/login'};
refresh().catch(e=>msg(e.message));
""")


WEB_LOGIN_PAGE = document("""<div class="top"><h1>网页登录 Twitch</h1><a href="/">返回 Dashboard</a></div><p>无需在你的设备安装软件。下方是服务器上为你临时创建的独立浏览器；点击画面、输入账号并完成 Twitch 验证。当前为试用功能，服务器 IP 或浏览器验证可能导致登录失败，此时可使用 Dashboard 中的本机助手。</p><p id="message">正在启动登录画面，请稍候…</p><canvas id="screen" width="1100" height="800" tabindex="0" aria-label="远程 Twitch 登录浏览器" style="width:100%;height:auto;border:1px solid #596078;border-radius:10px;background:white;outline:none;touch-action:none"></canvas><p>点击画面后可输入文字，支持 Tab、Enter、退格和粘贴。单次会话最多 10 分钟；关闭页面会销毁临时浏览器。仅此登录窗口的会话会交给你自己的掉宝实例，不会在页面或日志中展示 Twitch 会话。</p>""", """
const screen=document.querySelector('#screen'),ctx=screen.getContext('2d'),message=document.querySelector('#message');
let ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/portal/twitch-login/ws');
ws.binaryType='blob';const send=v=>{if(ws.readyState===1)ws.send(JSON.stringify(v))};
ws.onmessage=async e=>{if(typeof e.data==='string'){let v=JSON.parse(e.data);message.textContent=v.message||'';if(v.type==='done'){ws.close();setTimeout(()=>location.href='/',800)}}else{let image=await createImageBitmap(e.data);ctx.drawImage(image,0,0,1100,800);image.close()}};
ws.onclose=()=>{if(!message.textContent.includes('成功'))message.textContent='登录画面已关闭。可以刷新重试，或返回 Dashboard 使用本机助手。'};
function point(e){let r=screen.getBoundingClientRect();return{x:Math.max(0,Math.min(1100,(e.clientX-r.left)*1100/r.width)),y:Math.max(0,Math.min(800,(e.clientY-r.top)*800/r.height))}}
screen.onpointerdown=e=>{e.preventDefault();screen.focus();send({type:'down',...point(e)})};screen.onpointerup=e=>{e.preventDefault();send({type:'up',...point(e)})};screen.oncontextmenu=e=>e.preventDefault();
screen.addEventListener('wheel',e=>{e.preventDefault();send({type:'wheel',...point(e),delta:e.deltaY})},{passive:false});
screen.onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='v')return;e.preventDefault();if(e.ctrlKey||e.metaKey){if(e.key.toLowerCase()==='a')send({type:'key',key:'a',select_all:true});return}if(e.key.length===1)send({type:'text',text:e.key});else if(['Backspace','Tab','Enter','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Delete','Home','End'].includes(e.key))send({type:'key',key:e.key})};
screen.onpaste=e=>{e.preventDefault();send({type:'text',text:e.clipboardData.getData('text').slice(0,2000)})};
window.addEventListener('pagehide',()=>ws.close());
""")


def token_of(connection):
    auth = connection.headers.get("authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else connection.cookies.get(COOKIE, "")


def same_origin(connection) -> bool:
    origin = connection.headers.get("origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == connection.headers.get("host", "").lower()


def helper_source(source: Path) -> str:
    code = (source / "scripts" / "tdm_login_helper.py").read_text("utf-8-sig")
    start, end = code.index("def ensure_password("), code.index("def submit_session(")
    replacement = '''def ensure_password(base_url, password, prompt=getpass.getpass):
    username = input("HD Tool username: ").strip()
    for attempt in range(PASSWORD_TRIES):
        secret = (password if password is not None and attempt == 0 else prompt("HD Tool admin password: ")) if username.lower() == "admin" else ""
        req = urllib.request.Request(base_url.rstrip('/') + '/portal/api/login', data=json.dumps({'username': username, 'password': secret, 'helper': True}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)['token']
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 429):
                raise
            print('Platform login failed. Check username/password or try later.', file=sys.stderr)
    raise RuntimeError('HD Tool login failed.')


'''
    return (code[:start] + replacement + code[end:]).replace('"X-Fleet-Password": password', '"Authorization": "Bearer " + password')


def create_app(root: Path, source: Path, engine_factory=Engine, remote_factory=RemoteLoginManager) -> FastAPI:
    store = Store(root, source)
    engine = engine_factory(store)
    remote_logins = remote_factory()

    async def warmup():
        for user in list(store.users.values()):
            if user["enabled"] and user["running"]:
                try:
                    await engine.start(user)
                except Exception:
                    LOG.error("[TDM] 用户 %s 实例启动失败，请在用户中心重试", user["name"])

    @asynccontextmanager
    async def lifespan(app):
        async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                                         timeout=aiohttp.ClientTimeout(total=None, sock_connect=5), connector=aiohttp.TCPConnector(limit=100)) as client:
            app.state.client = client
            task = asyncio.create_task(warmup())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await engine.close()
                await remote_logins.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine = store, engine

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > 2_000_000:
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        bearer = request.headers.get("authorization", "").startswith("Bearer ")
        if request.method not in ("GET", "HEAD") and not bearer and not same_origin(request):
            # Only the local login helper may authenticate without an Origin header, using JSON.
            if not (path == "/portal/api/login" and not request.headers.get("origin") and request.headers.get("content-type", "").startswith("application/json")):
                return JSONResponse({"detail": "请求来源不匹配，请从本站页面操作"}, status_code=403)
        public = path in ("/portal/login", "/portal/api/login", "/healthz", "/login-helper.py")
        user = store.user(token_of(request))
        if not public and not user:
            if path == "/" or path == "/portal" or path.startswith("/__auth"):
                return RedirectResponse("/portal/login", status_code=302)
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        request.state.user = user
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": "multi-user"}

    @app.get("/portal/login")
    async def login_page(request: Request):
        if request.state.user:
            return RedirectResponse("/portal" if request.state.user["admin"] else "/", status_code=302)
        return HTMLResponse(LOGIN_PAGE)

    @app.get("/portal")
    async def home(request: Request):
        if not request.state.user["admin"]:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(HOME_PAGE)

    @app.get("/portal/twitch-login")
    async def web_login_page():
        return HTMLResponse(WEB_LOGIN_PAGE)

    @app.post("/portal/api/login")
    async def login(request: Request):
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "只接受 JSON")
        body = await request.json()
        if not isinstance(body.get("username"), str) or not isinstance(body.get("password", ""), str):
            raise HTTPException(400, "请输入用户名和密码")
        token = await store.login(body["username"], body.get("password", ""), request.client.host if request.client else "unknown")
        destination = "/portal" if store.user(token)["admin"] else "/"
        response = JSONResponse({"ok": True, "next": destination, **({"token": token} if body.get("helper") is True else {})})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=SESSION_SECONDS,
                            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return response

    @app.post("/portal/api/logout")
    async def logout(request: Request):
        store.sessions.pop(token_of(request), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    def public_user(user):
        return {"id": user["id"], "name": user["name"], "admin": user["admin"], "enabled": user["enabled"], "running": engine.running(user)}

    @app.get("/portal/api/me")
    async def me(request: Request):
        return public_user(request.state.user)

    def administrator(request):
        if not request.state.user["admin"]:
            raise HTTPException(403, "仅管理员可操作")

    @app.get("/portal/api/users")
    async def users(request: Request):
        administrator(request)
        return [public_user(u) for u in store.users.values()]

    @app.post("/portal/api/users")
    async def add_user(request: Request):
        administrator(request)
        body = await request.json()
        if not isinstance(body.get("username"), str):
            raise HTTPException(400, "请输入用户名")
        user = store.create(body["username"], body.get("password"))
        # Creation is durable even if the miner is temporarily unable to start.
        try:
            await engine.start(user)
        except Exception:
            LOG.error("[TDM] 新用户 %s 实例未启动，可从用户中心重试", user["name"])
        return public_user(user)

    @app.post("/portal/api/users/{user_id}")
    async def edit_user(user_id: str, request: Request):
        administrator(request)
        user = store.users.get(user_id)
        if not user or user["admin"]:
            raise HTTPException(400, "不能通过此接口修改管理员")
        body = await request.json()
        if "password" in body:
            raise HTTPException(400, "普通用户无需密码")
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise HTTPException(400, "enabled 必须是布尔值")
            user["enabled"] = body["enabled"]
            user["version"] += 1
        store.save()
        if not user["enabled"]:
            await engine.stop(user)
        elif user["running"]:
            try:
                await engine.start(user)
            except Exception:
                LOG.error("[TDM] 用户 %s 实例未启动，可从用户中心重试", user["name"])
        return public_user(user)

    @app.post("/portal/api/password")
    async def change_password(request: Request):
        user = request.state.user
        administrator(request)
        body = await request.json()
        old = body.get("old_password", "")
        if not isinstance(old, str) or not hmac.compare_digest(await asyncio.to_thread(password_hash, old[:128], user["salt"]), user["hash"]):
            raise HTTPException(400, "当前密码错误")
        set_password(user, body.get("password"))
        store.save()
        return {"ok": True}

    @app.post("/portal/api/instance")
    async def instance(request: Request):
        user = request.state.user
        action = (await request.json()).get("action")
        if action not in ("start", "stop", "restart"):
            raise HTTPException(400, "未知操作")
        user["running"] = action != "stop"
        store.save()
        if action in ("stop", "restart"):
            await engine.stop(user)
        if action != "stop":
            await engine.start(user)
        return {"ok": True}

    @app.get("/login-helper.py")
    async def helper():
        return Response(helper_source(source), media_type="text/x-python")

    def fleet_scope(user):
        return {n: u for n, u in enumerate(store.users.values(), 1) if user["admin"] or u["id"] == user["id"]}

    async def backend_json(user, path, data=None):
        backend = await engine.start(user)
        async with app.state.client.request("POST" if data is not None else "GET",
                    f"http://127.0.0.1:{backend['port']}{path}", json=data,
                    headers={"X-Fleet-Password": backend["password"]}, timeout=aiohttp.ClientTimeout(total=6)) as response:
            if response.status >= 400:
                raise HTTPException(502, "实例暂时不可用")
            return await response.json()

    @app.get("/api/instances")
    async def instances():
        # Legacy tabs navigate to raw internal ports. The platform selects instances by session instead.
        return {"instances": [], "autoprovision_enabled": False}

    @app.get("/api/languages")
    async def languages():
        return {"available": ["简体中文"], "current": "简体中文"}

    @app.get("/api/accounts")
    async def account_slots(request: Request):
        return {"accounts": [], "active": request.state.user["name"]}

    @app.get("/api/accounts/migration-hint")
    async def migration_hint():
        return {"show": False}

    @app.get("/api/accounts/overview")
    async def overview(request: Request):
        async def one(n, user):
            row = {"n": n, "label": user["name"], "base_url": "/", "reachable": True,
                   "login": None, "status_text": "已停止" if user["enabled"] else "已停用", "paused": True,
                   "watching": None, "drops_today": 0, "last_active": None, "error": None}
            if not user["enabled"] or not user["running"]:
                return row
            try:
                status, instance, channels, stats, history = await asyncio.gather(*[
                    backend_json(user, route) for route in ("/api/status", "/api/instance", "/api/channels", "/api/stats", "/api/drops-history")])
                row.update(login=instance.get("login"), status_text=status.get("status"), paused=status.get("paused", False))
                watching = next((c for c in channels.get("channels", []) if c.get("watching")), None)
                if watching:
                    row["watching"] = {"channel": watching.get("name"), "game": watching.get("game")}
                today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
                row["drops_today"] = sum(d.get("count", 0) for d in stats.get("by_day", []) if d.get("date") == today)
                if isinstance(history, list) and history:
                    row["last_active"] = history[0].get("timestamp")
            except Exception:
                row.update(reachable=False, error="实例暂时不可用，请在用户中心重启")
            return row
        rows = await asyncio.gather(*(one(n, u) for n, u in fleet_scope(request.state.user).items()))
        return {"accounts": rows, "bulk_editable_fields": {"games_to_watch": "优先游戏", "auto_add_excluded_games": "排除游戏"}}

    def fleet_targets(user, body):
        scope = fleet_scope(user)
        numbers = body.get("targets")
        if not isinstance(numbers, list) or not numbers or any(type(n) is not int or n not in scope for n in numbers):
            raise HTTPException(403, "只能操作你有权限管理的账号")
        return [(n, scope[n]) for n in dict.fromkeys(numbers)]

    @app.post("/api/accounts/bulk-settings")
    async def bulk_settings(request: Request):
        body = await request.json()
        targets = fleet_targets(request.state.user, body)
        field, values, mode = body.get("field"), body.get("values"), body.get("mode", "add")
        if field not in ("games_to_watch", "auto_add_excluded_games") or mode not in ("add", "remove", "replace") or not isinstance(values, list) or len(values) > 500 or any(not isinstance(v, str) or len(v) > 200 for v in values):
            raise HTTPException(400, "批量设置参数无效")
        values = list(dict.fromkeys(v.strip() for v in values if v.strip()))
        results = []
        for n, user in targets:
            try:
                current = (await backend_json(user, "/api/settings")).get(field) or []
                new = values if mode == "replace" else [v for v in current if v not in values] if mode == "remove" else list(dict.fromkeys(current + values))
                await backend_json(user, "/api/settings", {field: new})
                results.append({"n": n, "success": True, "count": len(new)})
            except Exception:
                results.append({"n": n, "success": False, "error": "实例暂时不可用"})
        return {"results": results}

    @app.post("/api/accounts/bulk-action")
    async def bulk_action(request: Request):
        body = await request.json()
        targets = fleet_targets(request.state.user, body)
        action = body.get("action")
        if action not in ("start", "pause", "drop_mining"):
            raise HTTPException(400, "未知操作")
        results = []
        for n, user in targets:
            try:
                routes = ["/api/pause"] if action == "pause" else ["/api/resume", "/api/reload" if action == "drop_mining" else "/api/idle-watch/switch"]
                for route in routes:
                    await backend_json(user, route, {})
                results.append({"n": n, "success": True})
            except Exception:
                results.append({"n": n, "success": False, "error": "实例暂时不可用"})
        return {"results": results}

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(path: str, request: Request):
        route = "/" + path
        user = request.state.user
        if route.startswith("/__auth"):
            return RedirectResponse("/portal", status_code=302)
        if route == "/api/auth/logout-twitch" and request.method == "POST":
            await engine.stop(user)
            (store.data(user) / "cookies.jar").unlink(missing_ok=True)
            user["running"] = True
            store.save()
            await engine.start(user)
            return {"success": True}
        readable = route in READ_PATHS or (route.startswith("/static/") and ".." not in route and "\\" not in route) or bool(re.fullmatch(r"/api/channel-points/[a-zA-Z0-9_]+", route)) or route == "/socket.io/"
        if not ((request.method in ("GET", "HEAD") and readable) or (request.method == "POST" and (route in WRITE_PATHS or route == "/socket.io/"))):
            raise HTTPException(403, "该功能由平台管理；请到 /portal 用户中心操作")
        data = await request.body()
        if len(data) > 2_000_000:
            raise HTTPException(413, "请求过大")
        if route == "/api/settings" and request.method == "POST":
            settings = json.loads(data)
            if not isinstance(settings, dict):
                raise HTTPException(400, "设置参数无效")
            settings["language"] = "简体中文"
            restricted = set(settings) - SAFE_SETTINGS
            current = await backend_json(user, "/api/settings") if restricted else {}
            if any(settings[key] != current.get(key) for key in restricted):
                raise HTTPException(403, "代理、通知地址和系统设置由管理员管理")
            data = json.dumps({k: v for k, v in settings.items() if k in SAFE_SETTINGS}).encode()
        backend = await engine.start(user)
        target = f"http://127.0.0.1:{backend['port']}{route}"
        headers = {"X-Fleet-Password": backend["password"], "Host": request.headers.get("host", "localhost"),
                   "X-Forwarded-Proto": request.headers.get("x-forwarded-proto", request.url.scheme)}
        for key in ("content-type", "accept", "accept-language"):
            if key in request.headers:
                headers[key] = request.headers[key]
        try:
            upstream = await app.state.client.request(request.method, target, params=list(request.query_params.multi_items()), data=data or None,
                                                       headers=headers, allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise HTTPException(502, "实例暂时不可用，请在用户中心重启")
        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "content-disposition", "content-encoding")}
        if route == "/" and request.method == "GET" and "text/html" in upstream.headers.get("Content-Type", ""):
            content = (await upstream.read()).decode("utf-8")
            upstream.release()
            return HTMLResponse(render_dashboard(content, user))

        async def stream():
            try:
                async for chunk in upstream.content.iter_chunked(65536):
                    yield chunk
            finally:
                upstream.release()
        return StreamingResponse(stream(), status_code=upstream.status, headers=response_headers)

    @app.websocket("/portal/twitch-login/ws")
    async def web_login_socket(socket: WebSocket):
        token = token_of(socket)
        user = store.user(token)
        if not user or not same_origin(socket):
            await socket.close(code=1008)
            return
        await socket.accept()
        browser = None
        tasks = []
        stage = "instance"
        try:
            await backend_json(user, "/api/status")
            stage = "browser-start"
            browser = await remote_logins.open(user["id"])
            stage = "browser-stream"
            await socket.send_json({"type": "status", "message": "请在下方 Twitch 页面完成登录；无需安装 Python。"})

            async def input_loop():
                while True:
                    text = await socket.receive_text()
                    if len(text) > 10_000:
                        raise ValueError("input too large")
                    message = json.loads(text)
                    if not isinstance(message, dict):
                        raise ValueError("invalid input")
                    await browser.input(message)

            async def frames():
                deadline = asyncio.get_running_loop().time() + 600
                while store.user(token) and asyncio.get_running_loop().time() < deadline:
                    cookies = await browser.cookies()
                    if cookies.get("auth-token"):
                        await backend_json(user, "/api/login/browser/helper-session", {"auth_token": cookies["auth-token"], "unique_id": cookies.get("unique_id", "")})
                        await socket.send_json({"type": "done", "message": "登录会话已提交成功，正在返回 Dashboard…"})
                        return
                    await socket.send_bytes(base64.b64decode(await browser.screenshot()))
                    await asyncio.sleep(.6)
                await socket.send_json({"type": "status", "message": "登录会话已过期或账号已停用，请重新登录。"})

            tasks = [asyncio.create_task(f()) for f in (input_loop, frames)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, ConnectionResetError):
            pass
        except Exception as error:
            LOG.warning("[TDM] 网页登录未完成，阶段 %s，异常类型 %s", stage, type(error).__name__)
            with contextlib.suppress(Exception):
                await socket.send_json({"type": "error", "code": type(error).__name__, "stage": stage, "message": "网页登录未完成。可能是窗口被关闭、服务器验证或浏览器启动失败；可重试或使用本机助手。"})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if browser:
                await remote_logins.close(user["id"], browser)
            with contextlib.suppress(Exception):
                await socket.close()

    @app.websocket("/socket.io/")
    async def websocket(socket: WebSocket):
        token = token_of(socket)
        user = store.user(token)
        if not user or not same_origin(socket):
            await socket.close(code=1008)
            return
        backend = await engine.start(user)
        url = f"http://127.0.0.1:{backend['port']}/socket.io/"
        try:
            async with app.state.client.ws_connect(url, params=list(socket.query_params.multi_items()), headers={"X-Fleet-Password": backend["password"]}) as remote:
                await socket.accept()

                async def to_remote():
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("text") is not None:
                            await remote.send_str(message["text"])
                        elif message.get("bytes") is not None:
                            await remote.send_bytes(message["bytes"])

                async def to_user():
                    async for message in remote:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await socket.send_text(message.data)
                        elif message.type == aiohttp.WSMsgType.BINARY:
                            await socket.send_bytes(message.data)
                        else:
                            return

                async def revoked():
                    while store.user(token):
                        await asyncio.sleep(.5)

                tasks = [asyncio.create_task(f()) for f in (to_remote, to_user, revoked)]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    with contextlib.suppress(Exception):
                        await socket.close()
        except (aiohttp.ClientError, RuntimeError):
            with contextlib.suppress(Exception):
                await socket.close(code=1011)

    return app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    source = Path(os.environ["TDM_SOURCE_DIR"])
    from check import IntegrationCheck

    if source.resolve() != Path(__file__).resolve().parents[2]:
        raise RuntimeError("TDM_SOURCE_DIR must identify this fork checkout")
    IntegrationCheck(source).run()
    uvicorn.run(create_app(Path(os.environ["TDM_DATA_DIR"]), source), host="0.0.0.0", port=int(os.environ.get("TDM_PORT", "8767")), access_log=False, proxy_headers=False)

