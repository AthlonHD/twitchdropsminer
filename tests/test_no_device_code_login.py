"""The device-code login was removed: the browser login (local window or the
login helper) is the only way in. Old device-code sessions are not reused."""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.auth.auth_state import _AuthState
from src.web import app as app_module

ROOT = Path(__file__).resolve().parents[1]


class TestDeviceCodeLoginIsGone(unittest.TestCase):
    def test_device_code_endpoints_no_longer_exist(self):
        with patch.object(app_module, "_is_setup_done", return_value=True), patch.object(
            app_module, "_get_password", return_value=""
        ):
            client = TestClient(app_module.app)
            for path in ("/api/login/device-code/start", "/api/login/device-code/cancel"):
                self.assertIn(client.post(path).status_code, (404, 405), path)

    def test_dashboard_has_no_code_login(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("device-code", html)
        self.assertNotIn("device_code", js)
        self.assertNotIn("device-code", js)

    def test_env_var_no_longer_selects_another_login(self):
        auth = _AuthState(MagicMock())
        auth._browser_login = AsyncMock(return_value="tok")
        with patch.dict("os.environ", {"TDM_LOGIN_METHOD": "device_code"}):
            self.assertEqual(asyncio.run(auth._browser_login()), "tok")
        self.assertFalse(hasattr(auth, "request_device_code_login"))
        self.assertFalse(hasattr(auth, "_device_code_login"))


class TestLoginStatusHasNoCode(unittest.IsolatedAsyncioTestCase):
    async def test_status_never_carries_a_device_code(self):
        from src.web.managers.login import LoginFormManager

        broadcaster = MagicMock()
        broadcaster.emit = AsyncMock()
        manager = LoginFormManager(broadcaster, MagicMock())
        self.assertFalse(hasattr(manager, "ask_enter_code"))
        await manager.start_browser_login_remote()
        self.assertNotIn("device_code", manager.get_status())
