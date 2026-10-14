"""A background-review fork must never enter the durable cross-process turn-lease wait on a lease
THIS process holds.

Verified failure this pins (source + retained ~/.hermes/logs, 2026-10-14): a review fork is a FULL
conversation (``agent/background_review.py::_run_review_fork`` ->
``review_agent.run_conversation``), so it takes the same durable turn lease the live turns take
(``agent/turn_facade.py`` -> ``agent.turn_facade_lease.admit_durable_turn_lease``). When the
session's ``session_turn_leases`` row is held by THIS process, that wait can only be ended by a turn
only this process can finish, yet the fork sits in it for up to ``LEASE_WAIT_SECONDS`` (1800s) and —
because the FIRST contended attempt notifies immediately
(``hermes_state_compression.acquire_session_turn_lease``: ``last_notice_at is None`` fires
``on_wait(0.0)``) — pushes "⏳ Another Hermes process is using this session…" / "⏳ Still waiting for
the other Hermes process on this session (Ns)…" into the user's chat. Turns with
``origin=background_review`` appear in exactly that wait context in the retained logs (48 review-fork
turns in the window).

The fix: ``agent.background_review.defer_review_for_same_process_lease`` — the review is re-queued on
the existing idle queue (``agent/review_idle_queue`` QUEUE, coalesced per session) and runs when the
session is free, or skipped outright when there is nothing to re-queue. The durable wait is never a
fallback, and a free session / a FOREIGN holder keep today's behaviour exactly.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent.background_review as bg  # noqa: E402
import run_agent  # noqa: E402  # imported at collection time: run_agent's import graph settles updates
from agent import review_idle_queue as riq  # noqa: E402
from agent.review_idle_queue import ReviewIdleQueue, _IDLE_SETTLE_S  # noqa: E402
from agent.turn_facade_lease import LEASE_WAIT_SECONDS, admit_durable_turn_lease  # noqa: E402
from hermes_state_pidns import holder_namespace_token  # noqa: E402

SESSION_ID = "20261014_005710_e6bd23"
SNAPSHOT = [{"role": "user", "content": "hi"}]


def _holder(pid: int | None = None) -> str:
    """A holder string in production's shape (``pid=…:pidns=…:turn=…:platform=…``)."""
    return (
        f"pid={os.getpid() if pid is None else pid}{holder_namespace_token()}"
        f":turn={SESSION_ID}:t:abcd:platform=discord"
    )


OURS = _holder()
FOREIGN_PID = _holder(pid=os.getpid() + 4242)


def _payload(**over) -> dict:
    """The re-queue payload the spawner puts on the run token (run_agent._spawn_background_review_now)."""
    kwargs = dict(
        messages_snapshot=list(SNAPSHOT), review_memory=True, review_skills=True,
        focus=None, task_cfg={}, explicit=False, _requeue_attempts=1,
    )
    kwargs.update(over)
    return kwargs


class _Fork:
    """The review fork ``build_cache_parity_fork`` would return for one attempt."""

    instances: list = []

    def __init__(self) -> None:
        self.calls: list = []
        self.released = False
        self._session_messages: list = []
        self._review_attended = None
        type(self).instances.append(self)

    def run_conversation(self, **kwargs):
        self.calls.append(kwargs)
        return {"final_response": "ok"}

    def release_clients(self) -> None:
        self.released = True


class _LeaseDb:
    """state.db surface: the lease-row holder read + the admission the fork would have entered."""

    def __init__(self, holder=None) -> None:
        self.holder = holder
        self.acquire_calls: list = []  # (session_id, kwargs) per admission attempt

    def session_turn_lease_holder(self, session_id):
        return self.holder

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.acquire_calls.append((session_id, kwargs))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(1.5)  # the real wait loop's notice (fires on the first contended attempt)
        return False  # a holder that never releases: the wait fails closed after its budget

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        return None

    def get_session(self, session_id):
        return {"id": session_id}


class _Queue(ReviewIdleQueue):
    """The real idle queue with its dispatcher thread and server probe replaced by seams."""

    def __init__(self, server_idle: bool = True) -> None:
        super().__init__()
        self.clock = {"t": 0.0}
        self._now = lambda: self.clock["t"]
        self._server_idle = lambda: server_idle
        self._ensure_thread = lambda: None


def _parent(db, spawn=None) -> SimpleNamespace:
    """The parent-agent surface ``_run_review_in_thread`` touches."""
    agent = SimpleNamespace(
        session_id=SESSION_ID,
        _session_db=db,
        statuses=[],
        _background_review_agent=None,
        _active_children=[],
        _REVIEW_REQUEUE_MAX_ATTEMPTS=3,
        _emit_auxiliary_failure=lambda *a, **k: None,
        _safe_print=lambda *a, **k: None,
        background_review_callback=None,
    )
    agent._emit_status = agent.statuses.append
    agent._emit_warning = agent.statuses.append
    agent._spawn_background_review_now = spawn if spawn is not None else (lambda **kw: None)
    return agent


def _spawning_parent(db, fork_factory):
    """A parent whose ``_spawn_background_review_now`` mirrors AIAgent's: fresh run token, real
    ``spawn_background_review_thread`` target, run synchronously (no real thread)."""
    spawns: list = []

    def spawn(**kwargs):
        spawns.append(dict(kwargs))
        attempts = kwargs.pop("_requeue_attempts", 0)  # AIAgent's own parameter, not the review's
        run = bg._BackgroundReviewRun()
        run.requeue_kwargs = dict(kwargs, _requeue_attempts=attempts + 1)
        target, _prompt = bg.spawn_background_review_thread(agent, **kwargs, review_run=run)
        target()

    agent = _parent(db, spawn=spawn)
    return agent, spawns


def _patch_fork(monkeypatch, fork_factory) -> None:
    """Everything between the worker's entry and the fork's provider phase, minus the fork build."""
    monkeypatch.setattr(
        bg, "build_cache_parity_fork",
        lambda agent, task_cfg, max_iterations=None: (fork_factory(), {"routed": False}, False),
    )
    monkeypatch.setattr(bg, "_review_tool_whitelist", lambda *a, **k: ({"skill_view"}, set()))
    monkeypatch.setattr(bg, "_parent_can_emit_tool_calls", lambda agent: True)
    monkeypatch.setattr("tools.terminal_tool.set_approval_callback", lambda *a, **k: None)
    monkeypatch.setattr(bg, "load_background_review_settings", lambda: (True, {}))


def _lease_agent(monkeypatch, db) -> SimpleNamespace:
    """Agent harness matching the façade's admission contract (see tests/agent/test_turn_facade_lease)."""
    monkeypatch.setattr("agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0))
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


@pytest.fixture(autouse=True)
def _clean_fork_instances():
    _Fork.instances = []
    yield
    _Fork.instances = []


# ── (i) same-process holder: not run, re-queued, no wait and no notice ────────────────────


def test_same_process_holder_is_requeued_and_never_enters_the_lease_wait(monkeypatch, caplog):
    fork = _Fork()
    db, agent, queue = _LeaseDb(OURS), None, _Queue()
    agent = _parent(db)
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, lambda: fork)
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload()

    with caplog.at_level(logging.INFO, logger=bg.logger.name):
        bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=run)

    # The review conversation never ran ...
    assert fork.calls == []
    # ... and it never reached admission, so there is no durable wait and no "Still waiting for the
    # other Hermes process on this session (Ns)" / "Another Hermes process is using this session"
    # notice (admit_durable_turn_lease's on_wait is the only producer of either).
    assert db.acquire_calls == []
    assert agent.statuses == []
    # The review was re-queued instead, under the session's key, with its exact payload.
    assert queue.pending_count() == 1
    with queue._lock:
        item = queue._pending[SESSION_ID]
    assert item.agent is agent
    assert item.kwargs == _payload()
    # One log line says what happened; nothing is published as if the review had run.
    assert "Background review re-queued" in caplog.text
    assert "Background review complete" not in caplog.text
    # Teardown still ran: off the parent's tracking slots, clients released.
    assert agent._background_review_agent is None
    assert fork.released is True


def test_a_failing_queue_skips_the_review_instead_of_waiting(monkeypatch, caplog):
    """The queue is best-effort: a raise means SKIP, never the durable wait."""
    fork = _Fork()
    db = _LeaseDb(OURS)
    agent = _parent(db)
    queue = _Queue()

    def _refuse(agent, session_key, kwargs):
        raise RuntimeError("queue is full")

    queue.enqueue = _refuse
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, lambda: fork)
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload()

    with caplog.at_level(logging.WARNING, logger=bg.logger.name):
        bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=run)

    assert fork.calls == []
    assert db.acquire_calls == []
    assert agent.statuses == []
    assert queue.pending_count() == 0
    assert "re-queue failed" in caplog.text


def test_a_review_without_a_requeue_payload_is_skipped(monkeypatch):
    """No payload to re-queue (direct/legacy caller): skip the review, never the wait."""
    fork = _Fork()
    db = _LeaseDb(OURS)
    agent = _parent(db)
    queue = _Queue()
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, lambda: fork)

    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=None)

    assert fork.calls == []
    assert queue.pending_count() == 0
    assert db.acquire_calls == []
    assert agent.statuses == []


def test_the_bounded_requeue_cap_drops_the_review_instead_of_waiting(monkeypatch):
    """A lease row this process never releases must not cycle the review through the queue."""
    fork = _Fork()
    db = _LeaseDb(OURS)
    agent = _parent(db)
    queue = _Queue()
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, lambda: fork)
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload(_requeue_attempts=4)  # > _REVIEW_REQUEUE_MAX_ATTEMPTS (3)

    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=run)

    assert fork.calls == []
    assert queue.pending_count() == 0
    assert db.acquire_calls == []
    assert agent.statuses == []


# ── (ii) free session: the review runs exactly as today ──────────────────────────────────


def test_a_free_session_runs_the_review_and_touches_no_queue(monkeypatch):
    fork = _Fork()
    db = _LeaseDb(None)
    agent = _parent(db)
    queue = _Queue()
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, lambda: fork)
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload()

    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=run)

    assert db.acquire_calls == []  # the guard read the row; nothing else touched the lease here
    assert queue.pending_count() == 0
    assert len(fork.calls) == 1
    assert "review please" in fork.calls[0]["user_message"]
    assert fork.calls[0]["conversation_history"] == SNAPSHOT
    assert agent.statuses == []
    # The request phase was admitted and published exactly once (unchanged behaviour).
    assert run.request_done.is_set()


def test_an_unreadable_lease_row_is_not_read_as_ours(monkeypatch):
    """Any failure this guard cannot prove is ours keeps today's behaviour: run the review."""
    fork = _Fork()

    class _Locked(_LeaseDb):
        def session_turn_lease_holder(self, session_id):
            raise RuntimeError("database is locked")

    db = _Locked(None)
    agent = _parent(db)
    monkeypatch.setattr(riq, "QUEUE", _Queue())
    _patch_fork(monkeypatch, lambda: fork)

    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=None)

    assert len(fork.calls) == 1


# ── (iii) the re-queued review runs once when the session frees ──────────────────────────


def test_the_requeued_review_runs_exactly_once_when_the_session_frees(monkeypatch):
    db = _LeaseDb(OURS)
    agent, spawns = _spawning_parent(db, _Fork)
    queue = _Queue()
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, _Fork)

    # Attempt #1 runs while THIS process holds the lease: it is handed to the queue, not run.
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload(review_skills=True, focus="keep the deploy workflow")
    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=run)

    assert queue.pending_count() == 1
    assert [f.calls for f in _Fork.instances] == [[]]
    assert spawns == []

    # The session frees. A live turn first: the idle gate holds the item.
    db.holder = None
    queue.note_turn_started()
    assert queue._pop_dispatchable() is None
    queue.note_turn_finished()
    assert queue._pop_dispatchable() is None  # settle window not elapsed yet

    queue.clock["t"] += _IDLE_SETTLE_S + 1
    item = queue._pop_dispatchable()
    assert item is not None
    queue._dispatch(item)

    # Exactly one re-queued run: the in-flight attempt stayed at calls=0, the copy ran once.
    assert len(spawns) == 1
    assert spawns[0]["messages_snapshot"] == SNAPSHOT
    assert spawns[0]["review_skills"] is True
    assert spawns[0]["focus"] == "keep the deploy workflow"
    assert spawns[0]["_requeue_attempts"] == 1  # the counter stored with the queued snapshot
    assert [len(f.calls) for f in _Fork.instances] == [0, 1]
    assert "keep the deploy workflow" in _Fork.instances[1].calls[0]["user_message"]
    # No duplicate is left behind.
    assert queue.pending_count() == 0
    assert queue._pop_dispatchable() is None


# ── (iv) a FOREIGN holder keeps the durable path, wait notice and all ────────────────────


def test_a_foreign_holder_is_untouched_and_still_takes_the_durable_wait(monkeypatch):
    fork = _Fork()
    db = _LeaseDb(FOREIGN_PID)
    agent = _parent(db)
    queue = _Queue()
    monkeypatch.setattr(riq, "QUEUE", queue)
    _patch_fork(monkeypatch, lambda: fork)
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload()

    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=run)

    # Another process holds the row: the review runs (the guard does not own that case) ...
    assert len(fork.calls) == 1
    assert queue.pending_count() == 0
    assert agent.statuses == []

    # ... and the admission it enters still waits the full cross-process budget and notifies.
    lease_agent = _lease_agent(monkeypatch, db)
    admission = _admit(lease_agent)

    assert [call[0] for call in db.acquire_calls] == [SESSION_ID]
    assert db.acquire_calls[0][1]["wait_seconds"] == LEASE_WAIT_SECONDS
    assert any("Still waiting for the other Hermes process" in s for s in lease_agent.statuses)
    assert admission.lease is None
    assert admission.early_result is not None
    assert admission.early_result["error"] == f"session_turn_lease_timeout:{SESSION_ID}"


def test_defer_review_for_same_process_lease_matrix(monkeypatch):
    """The guard itself: only a PROVABLY-ours holder hands the review off."""
    from agent.turn_facade_lease import holder_is_this_process, holder_process_pid

    local = _Queue()
    monkeypatch.setattr(riq, "QUEUE", local)
    run = bg._BackgroundReviewRun()
    run.requeue_kwargs = _payload()

    assert holder_is_this_process(OURS) is True
    assert holder_is_this_process(FOREIGN_PID) is False
    assert holder_process_pid(FOREIGN_PID) == os.getpid() + 4242

    assert bg.defer_review_for_same_process_lease(_parent(_LeaseDb(OURS)), run) is True
    assert local.pending_count() == 1
    # A foreign pid, a free row, no db, and a db without the lease reader: all False.
    assert bg.defer_review_for_same_process_lease(_parent(_LeaseDb(FOREIGN_PID)), run) is False
    assert bg.defer_review_for_same_process_lease(_parent(_LeaseDb(None)), run) is False
    assert bg.defer_review_for_same_process_lease(_parent(None), run) is False
    assert bg.defer_review_for_same_process_lease(_parent(SimpleNamespace()), run) is False
    assert local.pending_count() == 1  # only the one provably-ours hand-off


def test_the_guard_leaves_an_agent_without_the_lease_reader_alone(monkeypatch):
    """No durable-lease reader on the agent (partial shims, direct callers) -> unchanged path."""
    fork = _Fork()
    db = _LeaseDb(OURS)
    agent = _parent(db)
    del agent._session_db  # the row is unreachable: the guard must not guess
    monkeypatch.setattr(riq, "QUEUE", _Queue())
    _patch_fork(monkeypatch, lambda: fork)

    bg._run_review_in_thread(agent, list(SNAPSHOT), "review please", task_cfg={}, review_run=None)

    assert len(fork.calls) == 1


# ── reachability boundary, asserted on purpose ───────────────────────────────────────────


def test_a_persistence_disabled_agent_never_attempts_the_lease(monkeypatch):
    """What the guard sits BEHIND, pinned so the assumption cannot drift silently.

    ``build_cache_parity_fork`` unconditionally makes the automatic review fork
    ``_persist_disabled`` (and drops its ``_session_db``) — agent/background_review.py, the
    "PERSISTENCE ISOLATION" setup — and ``admit_durable_turn_lease`` returns BEFORE touching the
    lease for a persistence-disabled agent (agent/turn_facade_lease.py). So today's automatic
    review fork reaches its conversation with no durable lease at all: nothing to hand off, and
    the guard above is what covers a review conversation that DOES take one (a fork that keeps
    persistence — the shape the wait-reporting evidence is about — plus every future one). If the
    detach is ever removed, this test fails and the guard becomes the live path.
    """
    db = _LeaseDb(OURS)
    lease_agent = _lease_agent(monkeypatch, db)
    lease_agent._persist_disabled = True  # what build_cache_parity_fork sets on every review fork

    admission = _admit(lease_agent)

    assert db.acquire_calls == []  # no attempted acquisition: no wait, no notice
    assert admission.lease is None
    assert admission.early_result is None


# ── the spawner's payload is what the guard re-queues ────────────────────────────────────


def test_the_spawner_puts_the_exact_requeue_payload_on_the_run_token(monkeypatch):
    """``AIAgent._spawn_background_review_now`` hands the guard an exact copy of the spawn."""
    spawned: list = []
    agent = _parent(_LeaseDb(OURS))
    agent._spawn_background_review_now = lambda **kw: spawned.append(kw)  # the thread start seam

    class _Stop(BaseException):
        pass

    class _Thread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target

        def start(self):
            raise _Stop  # don't run the review: we only want the payload it was given

    monkeypatch.setattr(run_agent, "threading", SimpleNamespace(Thread=_Thread, Event=threading.Event))
    monkeypatch.setattr(bg, "_background_review_task_config", lambda *a, **k: {})
    bound = run_agent.AIAgent._spawn_background_review_now.__get__(agent)
    seen: list = []
    monkeypatch.setattr(
        bg, "prepare_background_review_run",
        lambda a: seen.append(bg._BackgroundReviewRun()) or seen[-1],
    )

    with pytest.raises(_Stop):
        bound(list(SNAPSHOT), review_memory=True, review_skills=True,
              focus="keep the deploy workflow", explicit=True)

    assert seen[0].requeue_kwargs == {
        "messages_snapshot": SNAPSHOT,
        "review_memory": True,
        "review_skills": True,
        "focus": "keep the deploy workflow",
        "task_cfg": None,
        "explicit": True,
        "_requeue_attempts": 1,
    }
