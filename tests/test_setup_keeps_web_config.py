import json

import pytest
from starlette.testclient import TestClient

from src.web import app as app_module


@pytest.fixture
def web_config(tmp_path, monkeypatch):
    path = tmp_path / "web_config.json"
    monkeypatch.setattr(app_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "_WEB_CONFIG_FILE", path)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    # A pre-provisioned account (copied account folder + active_account),
    # dashboard password not set up yet.
    path.write_text(json.dumps({"active_account": "Aj"}))
    return path


@pytest.mark.parametrize("form", [
    {"action": "set", "password": "pw123", "password2": "pw123"},
    {"action": "skip"},
])
def test_first_run_setup_keeps_active_account(web_config, form):
    # Overwriting the whole file dropped active_account, so the instance
    # silently fell back to data/settings.json defaults (empty watch list)
    # on its next start.
    client = TestClient(app_module.app)
    client.post("/__setup_post", data=form, follow_redirects=False)

    cfg = json.loads(web_config.read_text())
    assert cfg["active_account"] == "Aj"
    assert cfg["setup_done"] is True
