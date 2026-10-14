"""Spawn-time delivery target for background processes launched by a delegate child.

A ``delegate_task`` child runs in-process with its OWN session row. A background
process it launches used to stamp that child row's id as the completion target, so
a long build/dev server reported to a session nobody reads (and, when the thread
moved on, the completion was dropped entirely). The stamp now resolves a delegated
child's id to the parent session the child was spawned from — carried on the
delegation context, so no store lookup is involved; outside delegation the captured
id is used untouched.
"""

from types import SimpleNamespace

from agent.delegation_context import delegated_child_context
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
from tools.terminal_tool_background import _stamp_gateway_routing

THREAD_KEY = "agent:main:discord:thread:T1"


def _bind_session(session_id):
    return set_session_vars(
        platform="discord", chat_id="T1", chat_type="thread", thread_id="T1",
        user_id="U1", session_key=THREAD_KEY, session_id=session_id,
    )


def test_delegated_child_spawn_stamps_the_parent_session():
    tokens = _bind_session("sess_parent")
    try:
        with delegated_child_context("sess_child", parent_session_id="sess_parent"):
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


def test_non_delegated_spawn_keeps_the_captured_session_id():
    tokens = _bind_session("sess_child")
    try:
        proc = SimpleNamespace()
        _stamp_gateway_routing(proc, get_session_env)
        assert proc.parent_session_id == "sess_child"
    finally:
        clear_session_vars(tokens)


def test_child_context_without_a_parent_id_keeps_the_captured_id():
    """Child construction enters the context before the child has a session id."""
    tokens = _bind_session("sess_child")
    try:
        with delegated_child_context():
            proc = SimpleNamespace()
            _stamp_gateway_routing(proc, get_session_env)
        assert proc.parent_session_id == "sess_child"
    finally:
        clear_session_vars(tokens)


def test_nested_child_context_restores_the_outer_parent_id():
    tokens = _bind_session("sess_parent")
    try:
        with delegated_child_context("sess_child", parent_session_id="sess_parent"):
            with delegated_child_context("sess_grandchild"):
                proc = SimpleNamespace()
                _stamp_gateway_routing(proc, get_session_env)
            assert proc.parent_session_id == "sess_grandchild", (
                "a nested child context without a declared parent keeps its own captured id"
            )
            inner = SimpleNamespace()
            _stamp_gateway_routing(inner, get_session_env)
            assert inner.parent_session_id == "sess_parent", (
                "the outer child's parent id must come back on exiting the inner context"
            )
    finally:
        clear_session_vars(tokens)
