"""
Regression tests for a truncated channel_points_ts.json (a write interrupted by a
container stop): the Points Over Time history stopped recording forever, since
every later append failed to parse the file and silently gave up before
rewriting it.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.utils import json_save


def test_json_save_failure_keeps_previous_file(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"keep": 1}))

    with pytest.raises(TypeError):
        json_save(path, {"bad": object()})

    assert json.loads(path.read_text()) == {"keep": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_points_snapshot_recovers_from_truncated_history(tmp_path, monkeypatch):
    import src.services.message_handlers as mh

    points_file = tmp_path / "channel_points.json"
    monkeypatch.setattr(mh, "_get_points_file", lambda: points_file)
    ts_file = tmp_path / "channel_points_ts.json"
    ts_file.write_text('{"tigz": [{"ts": "2026-08-12T22:07:44+00:00", "balance": 90}, ')

    twitch = MagicMock()
    twitch.gql_request = AsyncMock(return_value={
        "data": {"community": {"channel": {"self": {"communityPoints": {"balance": 500}}}}}
    })
    twitch.gui._broadcaster.emit = AsyncMock()
    twitch.settings.discord_webhook_points = ""

    asyncio.run(mh.MessageHandlerService(twitch)._emit_channel_points("SpookyLoopz", 1))

    history = json.loads(ts_file.read_text())
    assert [s["balance"] for s in history["spookyloopz"]] == [500]
