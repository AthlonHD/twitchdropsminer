"""Follow-ups from the login redesign review."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth import browser_login
from src.auth.plain_chrome import PlainChrome

ROOT = Path(__file__).resolve().parents[1]


def _tdm_profile_dirs() -> set[str]:
    return {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("tdm-chrome-")}


class TestPlainChromeCleansUpOnFailedStart(unittest.IsolatedAsyncioTestCase):
    async def test_missing_binary_leaves_no_profile_dir(self):
        before = _tdm_profile_dirs()
        with patch("src.auth.plain_chrome.asyncio.create_subprocess_exec",
                   new=AsyncMock(side_effect=FileNotFoundError("chrome.exe"))):
            with self.assertRaises(FileNotFoundError):
                await PlainChrome("missing.exe", "about:blank").start()
        self.assertEqual(_tdm_profile_dirs(), before)

    async def test_local_login_falls_back_to_remote_when_the_binary_is_missing(self):
        manager = browser_login.BrowserLoginManager()
        with (
            patch.object(browser_login, "_detect_real_display", return_value=0),
            patch.object(browser_login, "_plain_chrome_binary", new=AsyncMock(return_value="missing.exe")),
            patch("src.auth.plain_chrome.asyncio.create_subprocess_exec",
                  new=AsyncMock(side_effect=FileNotFoundError("missing.exe"))),
        ):
            self.assertEqual(await manager.start(), "remote")
        await manager.stop()


class TestNoSandboxOnlyAsRoot(unittest.TestCase):
    def test_not_on_windows(self):
        with patch.object(browser_login, "_IS_WINDOWS", True):
            self.assertFalse(browser_login._needs_no_sandbox())

    def test_not_for_a_normal_linux_user(self):
        with patch.object(browser_login, "_IS_WINDOWS", False), \
             patch.object(browser_login.os, "geteuid", create=True, return_value=1000):
            self.assertFalse(browser_login._needs_no_sandbox())

    def test_as_root_in_a_container(self):
        with patch.object(browser_login, "_IS_WINDOWS", False), \
             patch.object(browser_login.os, "geteuid", create=True, return_value=0):
            self.assertTrue(browser_login._needs_no_sandbox())


class TestWindowsTriesEdgeBeforeBundledChromium(unittest.IsolatedAsyncioTestCase):
    async def test_edge_is_used_when_chrome_is_missing(self):
        def exists(path):
            return path.endswith("msedge.exe")

        with (
            patch.object(browser_login, "_IS_WINDOWS", True),
            patch.dict(os.environ, {"PROGRAMFILES": r"C:\PF", "PROGRAMFILES(X86)": r"C:\PF86", "LOCALAPPDATA": r"C:\LA"}),
            patch.object(browser_login.os.path, "exists", side_effect=exists),
        ):
            binary = await browser_login._plain_chrome_binary()
        self.assertTrue(binary.endswith("msedge.exe"))


class TestDockerImage(unittest.TestCase):
    def test_x11vnc_is_no_longer_installed(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("x11vnc", dockerfile)
        self.assertNotIn("x11vnc", compose)
        self.assertNotIn("noVNC", compose)


class TestHttpWarning(unittest.TestCase):
    def test_helper_panel_warns_on_unencrypted_remote_dashboards(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="login-helper-http-warning"', html)
        self.assertIn("window.location.protocol === 'http:'", js)


class TestPerInstanceLogFile(unittest.TestCase):
    def test_default_instance_keeps_tdm_log(self):
        from src.__main__ import _log_file_name

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TDM_PORT", None)
            self.assertEqual(_log_file_name(), "TDM.log")
            os.environ["TDM_PORT"] = "8080"
            self.assertEqual(_log_file_name(), "TDM.log")

    def test_other_ports_get_their_own_file(self):
        from src.__main__ import _log_file_name

        with patch.dict(os.environ, {"TDM_PORT": "8082"}):
            self.assertEqual(_log_file_name(), "TDM-8082.log")
