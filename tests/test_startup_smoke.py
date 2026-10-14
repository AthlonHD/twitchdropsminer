"""Start the real app as a process and wait until the web server comes up.

Catches what unit tests that import single functions miss, e.g. module-level
startup code calling a helper defined further down (a NameError at boot that
took both production instances down once)."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_app_boots_until_the_web_server_starts():
    port = _free_port()
    with tempfile.TemporaryDirectory() as data_dir:
        env = {
            **os.environ,
            "TDM_PORT": str(port),
            "TDM_DATA_DIR": data_dir,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        proc = subprocess.Popen(
            [sys.executable, "main.py"], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        seen: list[str] = []
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                seen.append(line)
                # Stop before the login step can open a browser window.
                if "Starting web server" in line:
                    break
        finally:
            proc.kill()
            proc.wait(timeout=15)
            for log in (ROOT / "logs").glob(f"TDM-{port}.log*"):
                try:
                    log.unlink(missing_ok=True)
                except PermissionError:  # Windows may still hold it for a moment
                    pass
    output = "".join(seen)
    assert "Starting web server" in output, output[-2000:]
    assert "Traceback" not in output, output[-2000:]
