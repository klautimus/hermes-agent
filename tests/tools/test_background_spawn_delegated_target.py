"""Spawn-time delivery target for background processes launched by a delegate child.

A ``delegate_task`` child runs in-process with its OWN session row. A background
process it launches used to stamp that child row's id as the completion target,
so a long build/dev server reported to a session nobody reads (and, when the
thread moved on, the completion was dropped entirely). The stamp now resolves a
delegated child's id to the parent session the child was spawned from; outside
delegation the captured id is used untouched.
"""

from types import SimpleNamespace

from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
from hermes_state import SessionDB
from tools.terminal_tool_background import _stamp_gateway_routing

THREAD_KEY = "agent:main:discord:thread:T1"


def _row(db, session_id, *, parent=None):
    db.create_session(
        session_id, "discord", session_key=THREAD_KEY, chat_id="T1",
        chat_type="thread", thread_id="T1", user_id="U1",
        parent_session_id=parent,
    )
    return session_id


def test_delegated_child_spawn_stamps_the_parent_session(tmp_path, monkeypatch):
    from agent.delegation_context import delegated_child_context

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    _row(db, "sess_parent")
    _row(db, "sess_child", parent="sess_parent")

    tokens = set_session_vars(
        platform="discord", chat_id="T1", chat_type="thread", thread_id="T1",
        user_id="U1", session_key=THREAD_KEY, session_id="sess_parent",
    )
    try:
        with delegated_child_context("sess_child"):
            assert get_session_env("HERMES_SESSION_ID") == "sess_child"
            proc = SimpleNamespace()
            _stamp_gateway_routing(proc, get_session_env)
        assert proc.parent_session_id == "sess_parent", (
            "a delegated child must stamp the parent session id, not its own row"
        )
        assert proc.watcher_chat_id == "T1"
        assert proc.watcher_platform == "discord"
    finally:
        clear_session_vars(tokens)

    db.close()


def test_non_delegated_spawn_keeps_the_captured_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    _row(db, "sess_parent")
    _row(db, "sess_child", parent="sess_parent")

    tokens = set_session_vars(
        platform="discord", chat_id="T1", chat_type="thread", thread_id="T1",
        user_id="U1", session_key=THREAD_KEY, session_id="sess_child",
    )
    try:
        proc = SimpleNamespace()
        _stamp_gateway_routing(proc, get_session_env)
        assert proc.parent_session_id == "sess_child"
    finally:
        clear_session_vars(tokens)

    db.close()


def test_delegated_child_spawn_survives_an_unreadable_session_db(tmp_path, monkeypatch):
    """Best-effort: no state.db (fresh home, secondary store) keeps the id."""
    from agent.delegation_context import delegated_child_context

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "absent"))

    tokens = set_session_vars(
        platform="discord", chat_id="T1", chat_type="thread", session_id="sess_parent",
    )
    try:
        with delegated_child_context("sess_child"):
            proc = SimpleNamespace()
            _stamp_gateway_routing(proc, get_session_env)
        assert proc.parent_session_id == "sess_child"
    finally:
        clear_session_vars(tokens)
