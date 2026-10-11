"""Account-specific channel bans are excluded without guessing from watch failures."""

import asyncio
import functools
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from src.config import MAX_INT
from src.exceptions import ExitRequest, GQLException
from src.models.channel import Channel, Stream
from src.models.game import Game
from src.services.channel_service import ChannelService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue


def _run_async(fn):
    """No pytest-asyncio here: run each coroutine test on its own loop."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


GAME = {"id": "1", "name": "Test Game"}


def _client():
    game = Game(GAME)
    campaign = SimpleNamespace(game=game, can_earn=lambda channel: True)
    client = SimpleNamespace(
        gui=SimpleNamespace(channels=MagicMock()),
        gql_request=AsyncMock(),
        inventory=[campaign],
        wanted_games=[game],
        watching_channel=AwaitableValue(),
        request=MagicMock(),
        request_games_update=MagicMock(),
    )
    client._channel_service = ChannelService(client)
    return client


def _channel(client, channel_id=1, *, acl=False):
    channel = Channel(client, id=channel_id, login=f"channel{channel_id}", acl_based=acl)
    channel._stream = Stream(channel, id=channel_id, game=GAME, viewers=100, title="Live")
    channel._stream.drops_enabled = True
    return channel


def _ban_response(channel_id, status):
    return {"data": {"user": {"id": str(channel_id), "self": {"banStatus": status}}}}


@_run_async
@pytest.mark.parametrize("acl", [False, True])
@pytest.mark.parametrize("permanent", [False, True])
async def test_confirmed_ban_is_filtered_and_cannot_be_manually_watched(acl, permanent):
    client = _client()
    banned = _channel(client, acl=acl)
    allowed = _channel(client, 2)
    client.gql_request.return_value = [
        _ban_response(2, None),
        _ban_response(1, {"isPermanent": permanent}),
    ]

    candidates = await client._channel_service.filter_banned_channels(iter([banned, allowed]))

    assert candidates == [allowed]
    assert banned.online  # A live broadcast alone does not grant this account access.
    assert not WatchService(client).can_watch(banned)
    assert WatchService(client).can_watch(allowed)
    client.watching_channel.set(banned)
    assert not await banned.send_watch()
    client.request.assert_not_called()


def test_no_ban_remains_eligible():
    client = _client()
    channel = _channel(client)
    channel.update_ban_status(_ban_response(1, None))

    assert not channel.banned
    assert WatchService(client).can_watch(channel)


def test_timeout_blocks_until_reported_expiry_and_does_not_expire_a_permanent_ban(monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("src.models.channel.time", lambda: now)
    client = _client()
    channel = _channel(client)
    expiry = datetime.fromtimestamp(now + 60, timezone.utc).isoformat()
    channel.update_ban_status(_ban_response(1, {"isPermanent": False, "expiresAt": expiry}))
    assert channel.banned
    assert not WatchService(client).can_watch(channel)
    now += 60
    assert not channel.banned
    assert WatchService(client).can_watch(channel)
    channel.update_ban_status(_ban_response(1, {"isPermanent": True, "expiresAt": expiry}))
    assert channel.banned


@pytest.mark.parametrize("expiry", [None, "invalid", "2026-10-09T00:00:00", 123])
def test_timeout_with_unknown_or_invalid_expiry_waits_for_explicit_no_ban(expiry):
    channel = _channel(_client())
    channel.update_ban_status(_ban_response(1, {"isPermanent": False, "expiresAt": expiry}))
    assert channel.banned
    channel.update_ban_status(_ban_response(1, None))
    assert not channel.banned


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": None},
        {"data": {"user": None}},
        {"data": {"user": {"id": "1", "self": None}}},
        {"data": {"user": {"id": "1", "self": {}}}},
        _ban_response(1, {}),
        _ban_response(1, {"isPermanent": "true"}),
        _ban_response(1, {"isPermanent": 1}),
        _ban_response(2, {"isPermanent": True}),
        {**_ban_response(1, {"isPermanent": True}), "errors": [{"message": "unavailable"}]},
    ],
)
def test_missing_invalid_or_foreign_response_does_not_change_known_status(response):
    client = _client()
    channel = _channel(client)
    channel.update_ban_status(response)
    assert not channel.banned

    channel.update_ban_status(_ban_response(1, {"isPermanent": True}))
    channel.update_ban_status(response)
    assert channel.banned


@_run_async
async def test_unban_refresh_reenables_channel_and_new_account_does_not_inherit_ban():
    client = _client()
    channel = _channel(client)
    channel.update_ban_status(_ban_response(1, {"isPermanent": True}))
    client.gql_request.return_value = [_ban_response(1, None)]

    assert await client._channel_service.filter_banned_channels([channel]) == [channel]
    assert WatchService(client).can_watch(channel)
    assert not _channel(_client()).banned


@_run_async
async def test_valid_unban_clears_all_rediscovered_and_retained_instances():
    client = _client()
    old, fresh = _channel(client), _channel(client)
    old.update_ban_status(_ban_response(1, {"isPermanent": True}))
    client.gql_request.return_value = [_ban_response(1, None)]

    assert await client._channel_service.filter_banned_channels(
        [fresh], retained_channels=[old, old],
    ) == [fresh]
    assert not old.banned and not fresh.banned
    assert len(client.gql_request.await_args.args[0]) == 1


@_run_async
async def test_failed_refresh_preserves_known_ban_in_fresh_instances():
    client = _client()
    old, fresh = _channel(client), _channel(client)
    old.update_ban_status(_ban_response(1, {"isPermanent": True}))
    client.gql_request.side_effect = TimeoutError()

    assert await client._channel_service.filter_banned_channels(
        [fresh], retained_channels=[old],
    ) == []
    assert old.banned and fresh.banned
    assert not WatchService(client).can_watch(old)
    assert not WatchService(client).can_watch(fresh)


@_run_async
async def test_service_retains_bans_across_rediscovery_failure_until_no_ban_response():
    client = _client()
    old, fresh = _channel(client), _channel(client)
    client.gql_request.return_value = [_ban_response(1, {"isPermanent": False})]
    assert await client._channel_service.filter_banned_channels([old]) == []
    client.gql_request.side_effect = TimeoutError()
    assert await client._channel_service.filter_banned_channels([fresh]) == []
    assert fresh.banned and client._channel_service.banned_channels == [fresh]
    client.gql_request.side_effect = None
    client.gql_request.return_value = [_ban_response(1, None)]
    assert await client._channel_service.filter_banned_channels([fresh]) == [fresh]
    assert not client._channel_service.banned_channels


@_run_async
async def test_timeout_expiry_callback_queues_discovery_and_clears_on_account_teardown(monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("src.models.channel.time", lambda: now)
    monkeypatch.setattr("src.services.channel_service.time", lambda: now)
    client = _client()
    channel = _channel(client)
    expiry = datetime.fromtimestamp(now + 60, timezone.utc).isoformat()
    client.gql_request.return_value = [_ban_response(1, {"isPermanent": False, "expiresAt": expiry})]
    captured = []
    loop = asyncio.get_running_loop()
    actual_schedule = loop.call_later

    def schedule(delay, callback, *args, **kwargs):
        if callback == client._channel_service._ban_expired:
            handle = MagicMock()
            captured.append((delay, callback, handle))
            return handle
        return actual_schedule(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", schedule)
    assert await client._channel_service.filter_banned_channels([channel]) == []
    assert captured[0][0] == 60
    now += 60
    captured[0][1]()
    assert not channel.banned and WatchService(client).can_watch(channel)
    client.request_games_update.assert_called_once_with()
    assert client._channel_service._ban_refresh is None
    client.gql_request.return_value = [_ban_response(1, {"isPermanent": False, "expiresAt": "2099-01-01T00:00:00Z"})]
    await client._channel_service.filter_banned_channels([channel])
    assert client._channel_service._ban_refresh is captured[-1][2]
    client._channel_service.clear_bans()
    captured[-1][2].cancel.assert_called_once_with()
    assert client._channel_service._ban_refresh is None and not client._channel_service.banned_channels


@_run_async
async def test_expired_timeout_is_discovered_again_after_failed_optional_check(monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("src.models.channel.time", lambda: now)
    monkeypatch.setattr("src.services.channel_service.time", lambda: now)
    client = _client()
    channel = _channel(client)
    client.gql_request.return_value = [_ban_response(1, {
        "isPermanent": False, "expiresAt": datetime.fromtimestamp(now + 60, timezone.utc).isoformat(),
    })]
    try:
        assert await client._channel_service.filter_banned_channels([channel]) == []
        now += 60
        client.gql_request.side_effect = TimeoutError()
        fresh = _channel(client)
        assert await client._channel_service.filter_banned_channels([fresh]) == [fresh]
        assert WatchService(client).can_watch(fresh)
        assert not client._channel_service.banned_channels
    finally:
        client._channel_service.clear_bans()


def test_known_ban_is_not_inherited_by_another_account_or_channel():
    client = _client()
    old = _channel(client)
    old.update_ban_status(_ban_response(1, {"isPermanent": True}))
    for unrelated in (_channel(client, 2), _channel(_client())):
        unrelated.inherit_ban(old)
        assert not unrelated.banned


@_run_async
async def test_discovery_checks_all_batches_and_preserves_response_channel_identity():
    client = _client()
    channels = [_channel(client, index) for index in range(1, 46)]

    async def responses(operations):
        assert len(operations) <= 20
        return [
            _ban_response(int(op["variables"]["channelID"]),
                          {"isPermanent": False} if int(op["variables"]["channelID"]) % 2 == 0 else None)
            for op in reversed(operations)
        ]

    client.gql_request.side_effect = responses
    candidates = await client._channel_service.filter_banned_channels(iter(channels))

    assert [channel.id for channel in candidates] == list(range(1, 46, 2))
    assert client.gql_request.await_count == 3


@_run_async
@pytest.mark.parametrize("failure", [GQLException("unavailable"), aiohttp.ClientError(), TimeoutError()])
async def test_optional_check_failure_preserves_unknown_and_confirmed_status(failure):
    client = _client()
    unknown = _channel(client)
    banned = _channel(client, 2)
    banned.update_ban_status(_ban_response(2, {"isPermanent": True}))
    client.gql_request.side_effect = failure

    assert await client._channel_service.filter_banned_channels([unknown, banned]) == [unknown]
    assert not unknown.banned
    assert banned.banned


@_run_async
async def test_malformed_and_errored_rows_do_not_abort_discovery_or_create_bans():
    client = _client()
    channel = _channel(client)
    client.gql_request.return_value = [
        None,
        {"data": "unavailable"},
        _ban_response([1], {"isPermanent": True}),
        {"data": {"user": {"id": [1], "self": {"banStatus": {"isPermanent": True}}}}},
        {**_ban_response(1, {"isPermanent": True}), "errors": [{"message": "unavailable"}]},
    ]

    assert await client._channel_service.filter_banned_channels([channel]) == [channel]
    assert not channel.banned


@_run_async
@pytest.mark.parametrize("action", ["cancel", "exit"])
async def test_check_drains_all_requests_and_propagates_cancellation_and_exit(action):
    client = _client()
    entered = asyncio.Event()
    requests = []
    cleaned = []

    async def blocked(operations):
        requests.append(asyncio.current_task())
        entered.set()
        try:
            if action == "exit":
                raise ExitRequest()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(asyncio.current_task())

    client.gql_request.side_effect = blocked
    parent = asyncio.create_task(client._channel_service.filter_banned_channels(
        [_channel(client, index) for index in range(1, 46)]
    ))
    await asyncio.wait_for(entered.wait(), 1)
    if action == "cancel":
        parent.cancel()
    with pytest.raises(asyncio.CancelledError if action == "cancel" else ExitRequest):
        await parent
    assert requests and all(request.done() for request in requests)
    assert len(cleaned) == len(requests)


@_run_async
async def test_empty_discovery_does_not_request_ban_status():
    client = _client()
    assert await client._channel_service.filter_banned_channels(iter([])) == []
    client.gql_request.assert_not_awaited()


def test_banned_current_channel_allows_fallback_at_tied_lowest_priority():
    client = _client()
    banned = _channel(client, acl=True)
    allowed = _channel(client, 2)
    banned.update_ban_status(_ban_response(1, {"isPermanent": True}))
    client.watching_channel.set(banned)
    client._channel_service.get_priority = lambda channel: MAX_INT

    assert WatchService(client).can_watch(allowed)
    assert WatchService(client).should_switch(allowed)


