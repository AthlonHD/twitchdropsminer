"""Deidentified diagnosis dump: sanitizing, snapshot shape, saving and API errors."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.diagnostics import Diagnostics
from src.utils.async_helpers import AwaitableValue
from src.web.diagnostics_api import DiagnosticsAPI


def _twitch(diagnostics):
    settings = SimpleNamespace(
        language="English", allow_unknown_link=True, dark_mode=False, inventory_list_view=False,
        connection_quality=3, minimum_refresh_interval_minutes=15, inventory_filters={},
        proxy="http://user:secret@proxy:1", games_to_watch=["Secret Game"], drop_name_blacklist=["x"],
        mining_benefits={"BADGE": True},
    )
    return SimpleNamespace(
        settings=settings, watching_channel=AwaitableValue(), channels={}, inventory=[], wanted_games=[],
        _channel_service=SimpleNamespace(banned_channels=[]), _state=None, _manual_target_channel=None,
        diagnostics=diagnostics,
    )


def test_sanitize_redacts_identity_and_secrets():
    diagnostics = Diagnostics(MagicMock())
    out = diagnostics.sanitize({
        "data": {"user": {"id": "12345", "login": "someone", "token": "abc", "viewersCount": 7}},
        "extensions": {"operationName": "ChannelBanStatus"},
    })
    text = json.dumps(out)
    assert "someone" not in text and "12345" not in text and "abc" not in text
    assert out["data"]["user"]["viewersCount"] == 7
    assert out["extensions"]["operationName"] == "ChannelBanStatus"


def test_record_http_never_raises_and_keeps_two_samples():
    diagnostics = Diagnostics(MagicMock())
    body = json.dumps([{"data": {"user": {"id": "1"}}}]).encode()
    for _ in range(3):
        diagnostics.record_http("POST", "https://gql.twitch.tv/gql", 200, body=body,
                                operations=[{"query": "query ChannelBanStatus($a: ID!) { x }"}])
    diagnostics.record_http("GET", object(), "not-an-int", body=b"\xff")
    entry = diagnostics.apis["twitch_gql:ChannelBanStatus"]
    assert entry["count"] == 3 and len(entry["samples"]) == 2


def test_save_writes_private_file_without_identities_and_prunes(tmp_path):
    async def run():
        diagnostics = Diagnostics(tmp_path)
        twitch = _twitch(diagnostics)
        names = []
        for _ in range(12):
            diagnostics._last_save = None
            names.append(await diagnostics.save(twitch, {"browser": "Chrome"}))
        return names

    names = asyncio.run(run())
    files = sorted((tmp_path / "diagnostics").glob("diagnosis-*.json"))
    assert len(files) == Diagnostics.MAX_FILES
    assert names[-1] == "diagnostics/" + files[-1].name or (tmp_path / names[-1]).exists()
    text = files[-1].read_text()
    assert "Secret Game" not in text and "secret@proxy" not in text and "user:" not in text
    data = json.loads(text)
    assert data["settings"]["proxy_configured"] is True


def test_save_is_rate_limited(tmp_path):
    async def run():
        diagnostics = Diagnostics(tmp_path)
        twitch = _twitch(diagnostics)
        await diagnostics.save(twitch)
        with pytest.raises(BlockingIOError):
            await diagnostics.save(twitch)

    asyncio.run(run())


def test_api_maps_errors_without_leaking_details(tmp_path):
    holder = {"client": None}
    app = FastAPI()
    app.include_router(DiagnosticsAPI(lambda: holder["client"]).router)
    http = TestClient(app)
    assert http.post("/api/diagnostics", json={}).status_code == 503
    diagnostics = Diagnostics(tmp_path)
    holder["client"] = _twitch(diagnostics)
    ok = http.post("/api/diagnostics", json={"browser": {"browser": "Chrome", "tab": "settings"}})
    assert ok.status_code == 200 and ok.json()["file"].startswith("diagnostics/")
    assert http.post("/api/diagnostics", json={}).status_code == 429
    assert http.post("/api/diagnostics", json={"bogus": 1}).status_code == 422
