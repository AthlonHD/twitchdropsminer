from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_has_the_helper_panel_and_no_novnc():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for element_id in ("login-helper-panel", "login-helper-command", "login-helper-copy", "login-helper-download"):
        assert f'id="{element_id}"' in html
    assert "browser-login-canvas-container" not in html
    assert "novnc" not in js.lower()
    assert "browser_login_ready" not in js
    assert not (ROOT / "web" / "static" / "vendor" / "novnc").exists()


def test_command_is_built_from_the_dashboard_address():
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.location.origin + API_BASE" in js
    assert "/login-helper.py" in js


def test_copy_works_without_the_async_clipboard_api():
    # navigator.clipboard only exists on https/localhost; a LAN http dashboard
    # must still copy (execCommand fallback) or at least select the text.
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.isSecureContext" in js
    assert "document.execCommand('copy')" in js


def test_hint_covers_python3_on_macos_and_linux():
    import json
    import re

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    hint = re.search(r'<p id="login-helper-py-hint">(.*?)</p>', html, re.S).group(1)
    assert "python3" in hint
    for lang in ("English", "Deutsch"):
        strings = json.loads((ROOT / "lang" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "python3" in strings["login"]["browser_login"]["helper_py_hint"]


def test_local_mode_shows_the_helper_as_an_alternative():
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data.login_helper === 'local'" in js
