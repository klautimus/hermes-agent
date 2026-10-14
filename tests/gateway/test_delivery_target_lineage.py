"""Delivery-time lineage resolution for synthetic completion deliveries.

A synthetic background-process / async-delegation completion carries the session
id captured at dispatch time and delivery never re-resolved it. When that id was
a delegation child's own row, or a session the thread had already superseded
(``/new`` -> ``end_reason='session_switch'``), the pre-flight classified it
"terminal" and the completion was dropped even though the user was still in the
same thread:

    "Background process proc_… completion targets permanently-gone session
     20261014_005710_09a970 (user boundary such as /new)"
    "Async delegation deleg_… targets permanently-gone session <id>;
     terminally dropping delivery"

The fix resolves the recorded target through its lineage at DELIVERY time: the
thread's current live session (routing key) first, then the nearest live
ancestor (a delegation child -> its ``parent_session_id``). An alive target is
returned untouched (no lookup at all), and a genuinely dead lineage still drops
with the same warning.

The spawn-time stamp is also fixed at the source: a delegate_task child stamps
its PARENT session id, because the child's own row is never where the user
reads messages.
"""

import asyncio
import logging
import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_state import AsyncSessionDB, SessionDB

THREAD_KEY = "agent:main:discord:thread:T1"
PARENT_CHAT = "T1"


def _db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _row(
    db, session_id, *, parent=None, ended=False, end_reason="session_switch",
    source="discord", session_key=THREAD_KEY, chat_id=PARENT_CHAT,
    chat_type="thread", thread_id=PARENT_CHAT, user_id="U1",
):
    """One gateway-shaped session row (live unless *ended*)."""
    db.create_session(
        session_id, source, session_key=session_key, chat_id=chat_id,
        chat_type=chat_type, thread_id=thread_id, user_id=user_id,
        parent_session_id=parent,
    )
    if ended:
        db.end_session(session_id, end_reason)
    return session_id


def _runner(adapter, session_db):
    def admit(event):
        event._gateway_accepted = True

    adapter.handle_message.side_effect = admit
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._session_db = session_db
    return runner


def _completion_evt(recorded_session_id, session_id="proc_1"):
    return {
        "type": "completion",
        "session_id": session_id,
        "session_key": THREAD_KEY,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": PARENT_CHAT,
        "thread_id": PARENT_CHAT,
        "user_id": "U1",
        "started_at": 1234.5,
        "command": "cargo build",
        "exit_code": 0,
        "completion_reason": "exited",
        "output": "ok\n",
        "parent_session_id": recorded_session_id,
    }


def _pinned_session_id(adapter):
    """The session the injected synthetic event pinned (``gateway_session_id``)."""
    assert adapter.handle_message.await_count == 1, (
        "expected exactly one injected synthetic event, got "
        f"{adapter.handle_message.await_count}"
    )
    event = adapter.handle_message.await_args.args[0]
    return event.metadata.get("gateway_session_id")


@pytest.fixture()
def adapter():
    return SimpleNamespace(handle_message=AsyncMock())


# ---------------------------------------------------------------------------
# (i) a completion recorded against a delegation child's own row -> the parent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("child_end_reason", ["agent_close", "session_switch"])
def test_delegation_child_target_is_delivered_to_parent_session(
    tmp_path, adapter, child_end_reason,
):
    """The subagent's own row is never where the user reads messages: the
    completion belongs to the session that spawned the child."""
    db = _db(tmp_path)
    _row(db, "sess_parent")
    _row(db, "sess_child", parent="sess_parent", ended=True, end_reason=child_end_reason)
    runner = _runner(adapter, AsyncSessionDB(db))

    result = asyncio.run(
        runner._deliver_completion_notification("done", _completion_evt("sess_child"))
    )

    assert result is True
    assert _pinned_session_id(adapter) == "sess_parent", (
        "a delegation child's completion must be delivered to the parent session "
        "where the user actually is, not terminally dropped"
    )


# ---------------------------------------------------------------------------
# (ii) a completion recorded against a superseded session -> current successor
# ---------------------------------------------------------------------------

def test_superseded_target_is_delivered_to_current_successor(tmp_path, adapter):
    """``/new`` rolled the thread to a new session: the result of work the user
    asked for must land in the thread's current session, not be dropped."""
    db = _db(tmp_path)
    _row(db, "sess_old", ended=True, end_reason="session_switch")
    _row(db, "sess_new", parent="sess_old")  # live successor of the same thread
    runner = _runner(adapter, AsyncSessionDB(db))

    result = asyncio.run(
        runner._deliver_completion_notification("done", _completion_evt("sess_old"))
    )

    assert result is True
    assert _pinned_session_id(adapter) == "sess_new"


def test_superseded_child_without_route_anchor_falls_back_to_live_ancestor(tmp_path, adapter):
    """A child with no chat anchor on the event (CLI/API origin) and a dead
    parent chain still resolves through the parent_session_id chain."""
    db = _db(tmp_path)
    _row(db, "sess_root")
    _row(db, "sess_child", parent="sess_root", ended=True, end_reason="session_switch")
    runner = _runner(adapter, AsyncSessionDB(db))
    evt = _completion_evt("sess_child")
    for field in ("platform", "chat_id", "chat_type", "thread_id", "user_id"):
        evt.pop(field)

    result = asyncio.run(runner._deliver_completion_notification("done", evt))

    assert result is True
    assert _pinned_session_id(adapter) == "sess_root"


# ---------------------------------------------------------------------------
# (iii) an alive target is untouched, with no lookup and no rewrite
# ---------------------------------------------------------------------------

def test_live_target_is_delivered_unchanged_without_a_lookup(tmp_path, adapter, monkeypatch):
    db = _db(tmp_path)
    _row(db, "sess_live")
    lookups = []

    real = db.find_session_by_origin

    def _spy(*args, **kwargs):
        lookups.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "find_session_by_origin", _spy)
    runner = _runner(adapter, AsyncSessionDB(db))
    evt = _completion_evt("sess_live")

    result = asyncio.run(runner._deliver_completion_notification("done", evt))

    assert result is True
    assert _pinned_session_id(adapter) == "sess_live"
    assert evt["parent_session_id"] == "sess_live", "alive target must not be rewritten"
    assert lookups == [], (
        "an alive recorded target must be delivered byte-for-byte as today: "
        "no lineage/routing lookup at all"
    )


def test_ended_but_routable_target_uses_the_routing_key(tmp_path, adapter):
    """Regression guard for the pre-existing idle-end contract: an idle-ended
    target is NOT a user boundary, so it must stay on the recorded id."""
    db = _db(tmp_path)
    _row(db, "sess_idle", ended=True, end_reason="idle_timeout")
    _row(db, "sess_other")
    runner = _runner(adapter, AsyncSessionDB(db))

    result = asyncio.run(
        runner._deliver_completion_notification("done", _completion_evt("sess_idle"))
    )

    assert result is True
    assert _pinned_session_id(adapter) == "sess_idle"


# ---------------------------------------------------------------------------
# (iv) a genuinely dead lineage still drops with the same warning
# ---------------------------------------------------------------------------

def test_dead_lineage_still_drops_with_the_same_warning(tmp_path, adapter, caplog):
    db = _db(tmp_path)
    _row(db, "sess_gone", ended=True, end_reason="session_switch")
    runner = _runner(adapter, AsyncSessionDB(db))

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            runner._deliver_completion_notification("done", _completion_evt("sess_gone"))
        )

    assert result is None
    adapter.handle_message.assert_not_awaited()
    assert any(
        "targets permanently-gone session sess_gone" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_unknown_lineage_still_drops(tmp_path, adapter, caplog):
    """A row that no longer exists and no live session for the route: drop."""
    db = _db(tmp_path)
    runner = _runner(adapter, AsyncSessionDB(db))

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            runner._deliver_completion_notification("done", _completion_evt("sess_deleted"))
        )

    assert result is None
    adapter.handle_message.assert_not_awaited()
    assert any(
        "targets permanently-gone session sess_deleted" in record.getMessage()
        for record in caplog.records
    )


def test_user_closed_target_without_a_live_successor_still_drops(tmp_path, adapter, caplog):
    """The shape of the original boundary fix: a session DB that knows nothing
    about routing must degrade to today's drop, not crash."""
    class _RouteAgnosticDB:
        async def get_session(self, session_id):
            return {"ended_at": 1786288000.0, "end_reason": "session_reset"}

        async def get_compression_tip(self, session_id):
            return None

    runner = _runner(adapter, _RouteAgnosticDB())

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            runner._deliver_completion_notification("done", _completion_evt("sess_closed"))
        )

    assert result is None
    adapter.handle_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# async-delegation path shares the same resolution
# ---------------------------------------------------------------------------
def test_async_delegation_completion_resolves_through_lineage(tmp_path, adapter):
    """The 9x 'Async delegation … terminally dropping' population: the target
    had rolled over before the result came back."""
    db = _db(tmp_path)
    _row(db, "sess_old", ended=True, end_reason="session_switch")
    _row(db, "sess_new", parent="sess_old")
    runner = _runner(adapter, AsyncSessionDB(db))
    evt = {
        "type": "async_delegation",
        "delegation_id": "",  # legacy row: no durable claim to settle
        "session_key": THREAD_KEY,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": PARENT_CHAT,
        "thread_id": PARENT_CHAT,
        "user_id": "U1",
        "parent_session_id": "sess_old",
        "status": "completed",
    }

    result = asyncio.run(runner._deliver_completion_notification("done", evt))

    assert result is True
    assert _pinned_session_id(adapter) == "sess_new"


def test_async_delegation_dead_lineage_still_drops_with_the_same_warning(
    tmp_path, adapter, caplog,
):
    db = _db(tmp_path)
    _row(db, "sess_gone", ended=True, end_reason="session_switch")
    runner = _runner(adapter, AsyncSessionDB(db))
    evt = {
        "type": "async_delegation",
        "delegation_id": "",
        "session_key": THREAD_KEY,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": PARENT_CHAT,
        "thread_id": PARENT_CHAT,
        "user_id": "U1",
        "parent_session_id": "sess_gone",
        "status": "completed",
    }

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(runner._deliver_completion_notification("done", evt))

    assert result is None
    adapter.handle_message.assert_not_awaited()
    assert any(
        "terminally dropping delivery" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# spawn-time capture lives in tests/tools/test_background_spawn_delegated_target.py
# ---------------------------------------------------------------------------
