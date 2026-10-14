"""An AUTOMATIC (internal) turn whose conversation lease is held by THIS process is queued
in-process instead of entering the durable cross-process wait.

Verified failure this pins (state.db + ~/.hermes/logs, 2026-10-14):

* ``session_turn_leases`` held ``conversation_id=20261014_005710_e6bd23`` with holder
  ``pid=630:pidns=4026532214:…:platform=discord`` — this process — while a background-process
  completion was injected as an internal event
  (``gateway.log: inbound message: … msg='[IMPORTANT: Background process proc_16db257ccbe2
  completed normally (exit code 0'``).
* The lease is keyed by the conversation (compression lineage root) while every busy guard in the
  gateway is keyed by ROUTING KEY, and ``switch_session`` makes key->id many-to-one (#64934) — so
  ``_is_session_running`` reported the key idle and a SECOND turn for the same session was started.
* That turn sat in ``acquire_session_turn_lease``'s wait for up to ``LEASE_WAIT_SECONDS`` (1800s)
  emitting "Still waiting for the other Hermes process on this session (Ns)…" every 15s
  (``agent.log.1: session turn lease wait aborted by interrupt`` ×9) and was finally dropped
  (``response ready: … time=973.7s api_calls=0`` / "Your message was not processed").

The blocking turn runs in THIS process, so no other process can end the wait: the automatic event
belongs in the adapter's in-process pending queue behind the live turn, which the post-turn drain
(``gateway/run_turn.py::_run_agent_drain_pending`` → ``_run_agent_queued_followup``) runs as the
next turn. Cross-process contention (a foreign pid/pidns holder) keeps today's behaviour exactly.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.turn_facade_lease import (
    LEASE_TTL_SECONDS,
    LEASE_WAIT_SECONDS,
    admit_durable_turn_lease,
    holder_is_this_process,
    holder_process_pid,
    same_process_holds_session_turn_lease,
)
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state_pidns import holder_namespace_token, pid_namespace_id

SESSION_ID = "20261014_005710_e6bd23"
SESSION_KEY = "agent:main:discord:thread:1558931180240244937:1558931180240244937"


def _holder(pid: int | None = None) -> str:
    """A holder string in production's shape (``pid=…:pidns=…:turn=…:platform=…``)."""
    return (
        f"pid={os.getpid() if pid is None else pid}{holder_namespace_token()}"
        f":turn={SESSION_ID}:t:abcd:platform=discord"
    )


OURS = _holder()
FOREIGN_PID = _holder(pid=os.getpid() + 4242)


class _LeaseDb:
    """Minimal SessionDB surface: the lease-row holder read plus admission accounting."""

    def __init__(self, holder=None, acquire_result=False):
        self.holder = holder
        self.acquire_result = acquire_result
        self.acquire_calls: list = []  # (session_id, kwargs) per admission attempt

    def session_turn_lease_holder(self, session_id):
        return self.holder

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.acquire_calls.append((session_id, kwargs))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(1.5)  # the real wait loop's "Still waiting…" notice, once per call
        return self.acquire_result

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        return None

    def get_session(self, session_id):
        return {"id": session_id}


def _agent(monkeypatch, db):
    """Agent stub matching the façade's admission contract (see tests/agent/test_turn_facade_lease)."""
    monkeypatch.setattr(
        "agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0)
    )
    agent = SimpleNamespace(
        _session_db=db,
        session_id=SESSION_ID,
        _persist_disabled=False,
        _interrupt_requested=False,
        _interrupt_message=None,
        _execution_thread_id=None,
        _session_turn_lease_refresh_interval=60.0,
        statuses=[],
    )
    agent._emit_status = agent.statuses.append
    agent._emit_warning = agent.statuses.append
    agent._touch_activity = lambda *a, **k: None
    agent._liveness_activity_lock = lambda: threading.Lock()
    return agent


def _admit(agent):
    return admit_durable_turn_lease(
        agent,
        session_id=SESSION_ID,
        relay_turn_id=f"{SESSION_ID}:t:abcd",
        task_context={"session_id": SESSION_ID, "task_id": "t", "platform": "discord"},
        conversation_history=None,
    )


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="1558931180240244937",
        user_id="klauts",
        user_name="klauts",
        chat_type="channel",
        thread_id="1558931180240244937",
    )


class _PendingAdapter:
    """The adapter-side in-process queue the gateway drains: ``_pending_messages`` by session key."""

    def __init__(self):
        self._pending_messages: dict = {}


def _runner(db, adapter):
    runner = object.__new__(GatewayRunner)
    runner._session_db = db  # property setter pins the handle
    runner._delivery_adapter_for = lambda source: adapter
    runner._peek_session_state = lambda session_key: None  # no /queue overflow in these fixtures
    return runner


def _internal_event() -> MessageEvent:
    """The shape gateway/run_notifications.py injects for a process completion."""
    return MessageEvent(
        text="[IMPORTANT: 1 background processes completed for this session. …]",
        message_type=MessageType.TEXT,
        source=_source(),
        internal=True,
        metadata={"notification_origin": "process_registry_synthetic"},
    )


# ── same-process identity from the existing holder string ────────────────────────────────


def test_holder_is_this_process_requires_the_same_pid_and_namespace():
    assert holder_is_this_process(OURS) is True
    assert holder_is_this_process(FOREIGN_PID) is False
    assert holder_process_pid(FOREIGN_PID) == os.getpid() + 4242
    assert holder_is_this_process("") is False
    assert holder_is_this_process("turn=abc") is False

    ns = pid_namespace_id()
    if ns is None:
        return  # platform without PID namespaces: nothing to disambiguate
    # A sibling namespace may legally carry our pid NUMBER: the pidns stamp must veto it.
    assert holder_is_this_process(
        f"pid={os.getpid()}:pidns={int(ns) + 1}:turn={SESSION_ID}:platform=discord"
    ) is False
    # Unstamped (pre-upgrade) rows are STRICT policy in hermes_state_pidns: never read as ours.
    assert holder_is_this_process(
        f"pid={os.getpid()}:turn={SESSION_ID}:platform=discord"
    ) is False


def test_same_process_holds_session_turn_lease_matrix():
    assert same_process_holds_session_turn_lease(_LeaseDb(OURS), SESSION_ID) is True
    assert same_process_holds_session_turn_lease(_LeaseDb(FOREIGN_PID), SESSION_ID) is False
    assert same_process_holds_session_turn_lease(_LeaseDb(None), SESSION_ID) is False
    assert same_process_holds_session_turn_lease(_LeaseDb(OURS), "") is False
    assert same_process_holds_session_turn_lease(None, SESSION_ID) is False
    # No reader (legacy / partial shim) or a locked read: never guess "ours".
    assert same_process_holds_session_turn_lease(SimpleNamespace(), SESSION_ID) is False

    class _Locked(_LeaseDb):
        def session_turn_lease_holder(self, session_id):
            raise RuntimeError("database is locked")

    assert same_process_holds_session_turn_lease(_Locked(), SESSION_ID) is False


# ── (a) same-process automatic turn: queued in-process, no wait, no notice ───────────────


def test_same_process_internal_event_is_queued_and_no_lease_attempt_happens():
    db, adapter = _LeaseDb(OURS), _PendingAdapter()
    runner = _runner(db, adapter)
    event = _internal_event()

    assert runner._defer_internal_event_behind_live_turn(event, SESSION_KEY, SESSION_ID) is True
    # Queued in the SAME in-process slot a busy follow-up uses (drained by the live turn).
    assert adapter._pending_messages[SESSION_KEY] is event
    assert event._gateway_accepted is True
    # No admission attempt at all, hence no 1800s wait and no "Still waiting…" notice (the only
    # producer of that notice is admit_durable_turn_lease's on_wait, reached only via acquire).
    assert db.acquire_calls == []


@pytest.mark.asyncio
async def test_automatic_turn_starts_no_turn_work_at_the_call_site():
    """The guard runs before any turn work (history load / typing / agent build)."""
    db, adapter = _LeaseDb(OURS), _PendingAdapter()
    runner = _runner(db, adapter)
    source, event = _source(), _internal_event()
    runner._hmwa_resolve_session = AsyncMock(
        return_value=(source, SimpleNamespace(session_id=SESSION_ID), SESSION_KEY)
    )
    runner._hmwa_prepare_turn = AsyncMock(
        side_effect=AssertionError("no turn work may start for a deferred automatic turn")
    )

    result = await runner._handle_message_with_agent(event, source, SESSION_KEY, 1)

    assert result is None
    runner._hmwa_prepare_turn.assert_not_awaited()
    assert adapter._pending_messages[SESSION_KEY] is event
    assert db.acquire_calls == []


def test_a_user_message_is_never_deferred_off_the_lease_path():
    """Only automatic/internal turns are queued: user-message semantics are untouched."""
    db, adapter = _LeaseDb(OURS), _PendingAdapter()
    runner = _runner(db, adapter)
    event = _internal_event()
    event.internal = False

    assert runner._defer_internal_event_behind_live_turn(event, SESSION_KEY, SESSION_ID) is False
    assert adapter._pending_messages == {}


def test_queue_cap_falls_back_to_the_durable_path_instead_of_dropping():
    """If the in-process queue refuses the event, keep the durable path: never a silent drop."""
    db, adapter = _LeaseDb(OURS), _PendingAdapter()
    runner = _runner(db, adapter)
    # The /queue cap (or any other refusal) makes the queue helper return without storing the event.
    runner._queue_or_replace_pending_event = lambda session_key, event: None
    event = _internal_event()

    assert runner._defer_internal_event_behind_live_turn(event, SESSION_KEY, SESSION_ID) is False
    assert adapter._pending_messages == {}


def test_missing_adapter_falls_back_to_the_durable_path():
    db = _LeaseDb(OURS)
    runner = _runner(db, None)  # no in-process queue to reach
    assert runner._defer_internal_event_behind_live_turn(_internal_event(), SESSION_KEY, SESSION_ID) is False


# ── (b) foreign holder: cross-process exclusion and its wait notice are preserved ────────


def test_foreign_pid_holder_is_not_deferred_and_still_takes_the_durable_wait(monkeypatch):
    db, adapter = _LeaseDb(FOREIGN_PID), _PendingAdapter()
    runner = _runner(db, adapter)
    event = _internal_event()

    assert runner._defer_internal_event_behind_live_turn(event, SESSION_KEY, SESSION_ID) is False
    assert adapter._pending_messages == {}

    # A different process holds the row: admission still waits the full budget and fails closed.
    agent = _agent(monkeypatch, db)
    admission = _admit(agent)

    assert [call[0] for call in db.acquire_calls] == [SESSION_ID]
    assert db.acquire_calls[0][1]["wait_seconds"] == LEASE_WAIT_SECONDS
    assert any("Still waiting for the other Hermes process" in s for s in agent.statuses)
    assert admission.lease is None
    assert admission.early_result is not None
    assert admission.early_result["error"] == f"session_turn_lease_timeout:{SESSION_ID}"
    assert admission.early_result["failure_reason"] == "session_busy"


def test_lease_admission_is_unchanged_for_a_same_process_holder(monkeypatch):
    """Boundary of the fix, asserted on purpose.

    The in-process queue is adapter/runner state (``adapter._pending_messages``,
    gateway/platforms/base.py) and is unreachable from ``agent/turn_facade_lease.py`` — an AIAgent
    holds no reference to the gateway runner or its adapters. The deferral is therefore enforced at
    the gateway's turn entry (before admission); the lease itself keeps its cross-process authority,
    so a turn that still reaches admission behaves exactly as before (ttl/wait/notice).
    """
    db = _LeaseDb(OURS)
    agent = _agent(monkeypatch, db)
    admission = _admit(agent)

    assert db.acquire_calls[0][1]["ttl_seconds"] == LEASE_TTL_SECONDS
    assert db.acquire_calls[0][1]["wait_seconds"] == LEASE_WAIT_SECONDS
    assert admission.early_result is not None
