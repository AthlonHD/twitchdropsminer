import asyncio
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.browser_login import (
    DISPLAY_NUMBER_RANGE_START,
    _detect_real_display,
    sweep_orphaned_processes,
)

from src.auth import browser_login as _browser_login_module

_windows_patch = patch.object(_browser_login_module, "_IS_WINDOWS", False)


def setUpModule():
    # These tests exercise the Linux/X11 paths; on a Windows test machine the
    # Windows shortcut (real desktop, installed Chrome) would bypass them.
    _windows_patch.start()


def tearDownModule():
    _windows_patch.stop()


def _mock_process():
    proc = MagicMock()
    proc.pid = 12345
    proc.terminate = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestDetectRealDisplay(unittest.TestCase):
    """
    start() opens an ordinary visible Chromium window directly on a real
    desktop display when one is already available (home/NAS deployments
    with DISPLAY passed through), instead of spinning up Xvfb/x11vnc/noVNC
    -- the same "real browser on a real device" signal Twitch's integrity
    check wants, with nothing extra to download or run since it's already
    the same machine. _detect_real_display is the gate for that path, so
    it must never mistake one of our own virtual displays, or a stale
    DISPLAY pointing at nothing, for a real one.
    """

    def test_none_when_display_env_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_detect_real_display())

    def test_none_when_display_env_is_not_a_display_string(self):
        with patch.dict("os.environ", {"DISPLAY": "not-a-display"}, clear=True):
            self.assertIsNone(_detect_real_display())

    def test_none_for_our_own_reserved_virtual_display_range(self):
        # e.g. a crashed previous attempt's DISPLAY leaking into this
        # process's own environment -- must never be treated as real.
        reserved = f":{DISPLAY_NUMBER_RANGE_START}"
        with (
            patch.dict("os.environ", {"DISPLAY": reserved}, clear=True),
            patch("src.auth.browser_login.os.path.exists", return_value=True),
        ):
            self.assertIsNone(_detect_real_display())

    def test_none_when_no_matching_x11_socket_exists(self):
        with (
            patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True),
            patch("src.auth.browser_login.os.path.exists", return_value=False),
        ):
            self.assertIsNone(_detect_real_display())

    def test_returns_the_display_number_when_a_real_socket_exists(self):
        with (
            patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True),
            patch("src.auth.browser_login.os.path.exists", return_value=True) as mock_exists,
        ):
            self.assertEqual(_detect_real_display(), 0)
            mock_exists.assert_called_once_with("/tmp/.X11-unix/X0")

    def test_handles_a_screen_suffix_like_colon_zero_dot_zero(self):
        with (
            patch.dict("os.environ", {"DISPLAY": ":0.0"}, clear=True),
            patch("src.auth.browser_login.os.path.exists", return_value=True),
        ):
            self.assertEqual(_detect_real_display(), 0)


class TestSweepOrphanedProcesses(unittest.IsolatedAsyncioTestCase):
    """
    sweep_orphaned_processes() had no coverage at all, which is how a real
    regression survived two review rounds: the pattern for one tracked
    process type changed while the sweep kept looking for the old form, so
    that process type was never swept again. Pin the patterns against the
    exact command lines BrowserLoginManager.start() generates -- see its
    Xvfb/x11vnc argv lists -- so a similar drift fails a test immediately.
    """

    OURS = {
        4001: "Xvfb :90 -screen 0 1280x800x24",
        4002: "x11vnc -display :90 -rfbport 5990 -localhost -nopw -forever -shared -quiet",
    }
    NOT_OURS = {
        5001: "Xvfb :0 -screen 0 1920x1080x24",
        5002: "x11vnc -display :0 -rfbport 5900 -localhost",
        5004: "/usr/bin/python3 main.py",
    }

    def _fake_pgrep(self, table):
        """Stand in for `pgrep -af <pattern>`, applying the pattern to a
        table of realistic command lines the way pgrep -f actually would."""

        async def create_subprocess_exec(*argv, **kwargs):
            self.assertEqual(argv[0], "pgrep")
            self.assertEqual(argv[1], "-af")
            pattern = argv[2]
            lines = [
                f"{pid} {cmdline}"
                for pid, cmdline in table.items()
                if re.search(pattern, cmdline) is not None
            ]
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(("\n".join(lines) + "\n").encode(), b""))
            return proc

        return create_subprocess_exec

    async def test_kills_our_own_orphans_and_frees_their_display_slot(self):
        table = {**self.OURS, **self.NOT_OURS}

        def kill_side_effect(pid, sig):
            # The SIGTERM itself succeeds; the liveness check (signal 0)
            # that follows finds the process already gone -- same as a
            # real orphan actually dying, and keeps this test from
            # burning the real grace-period sleep waiting it out.
            if sig == 0:
                raise ProcessLookupError()

        with (
            patch("asyncio.create_subprocess_exec", new=self._fake_pgrep(table)),
            patch("src.auth.browser_login.os.kill", side_effect=kill_side_effect) as mock_kill,
            patch("src.auth.browser_login._release_display_number") as mock_release,
            # A real orphan was re-parented to init when its TDM process died.
            patch("src.auth.browser_login._parent_pid", return_value=1),
        ):
            killed = await sweep_orphaned_processes()

        killed_pids = {call.args[0] for call in mock_kill.call_args_list}
        self.assertEqual(killed_pids, set(self.OURS))
        self.assertEqual(killed, len(self.OURS))
        # SIGKILL leaves /tmp/.X90-lock behind, which would otherwise burn
        # that display slot permanently.
        mock_release.assert_called_once_with(90)

    async def test_ignores_processes_outside_the_reserved_ranges(self):
        with (
            patch("asyncio.create_subprocess_exec", new=self._fake_pgrep(self.NOT_OURS)),
            patch("src.auth.browser_login.os.kill") as mock_kill,
            patch("src.auth.browser_login._release_display_number") as mock_release,
        ):
            killed = await sweep_orphaned_processes()

        self.assertEqual(killed, 0)
        mock_kill.assert_not_called()
        mock_release.assert_not_called()

    async def test_spares_processes_another_running_instance_still_owns(self):
        # Two instances on one host (pm2 tdm + tdm2): the second one's
        # startup sweep killed the first one's Xvfb mid-mint (seen live).
        with (
            patch("asyncio.create_subprocess_exec", new=self._fake_pgrep(self.OURS)),
            patch("src.auth.browser_login.os.kill") as mock_kill,
            patch("src.auth.browser_login._release_display_number") as mock_release,
            patch("src.auth.browser_login._parent_pid", return_value=4242),
        ):
            killed = await sweep_orphaned_processes()

        self.assertEqual(killed, 0)
        mock_kill.assert_not_called()
        mock_release.assert_not_called()


class TestRemoteLoginMode(unittest.IsolatedAsyncioTestCase):
    async def test_remote_login_never_opens_local_browser(self):
        with (
            patch.dict("os.environ", {"TDM_REMOTE_LOGIN": "1"}),
            patch.object(_browser_login_module, "_detect_real_display", return_value=0) as display,
            patch.object(_browser_login_module, "PlainChrome") as chrome,
        ):
            manager = _browser_login_module.BrowserLoginManager()
            self.assertEqual(await manager.start(), "remote")
            chrome.assert_not_called()
            display.assert_not_called()
            self.assertTrue(manager.in_progress)
            await manager.stop()


if __name__ == "__main__":
    unittest.main()
