"""skill_view repeat-view dedup registry: per-task cache of (skill name, file_path) ->
(skill file mtime+size). A repeat view of an UNCHANGED file returns a short stub — the earlier
tool result already carries the content verbatim. Cleared via ``reset_skill_view_dedup()`` on
context compression AND on a committed proactive tool-result prune, because both replace the
original content with a one-line marker.

DEDUP IS ONLY SOUND WHILE THE EARLIER RESULT STILL CARRIES THE CONTENT. The reset calls cover
the boundaries we know about; they cannot cover every path that reduces a tool result to a
marker. A stub is therefore only served for bodies the compressor keeps VERBATIM — see
``_prune_destroyable_floor`` below. For a body at or above that floor the stub is a lockout:
it tells the model the earlier result "is still current and complete" when that result is a
``[SKILL_PRUNED: ...]`` marker asking for exactly the reload the stub refuses, so the skill's
instructions are unrecoverable for the rest of the session while the model believes it holds
them.
"""

import json
import os
import threading
from typing import Dict

_skill_view_tracker: Dict[str, Dict[tuple, tuple]] = {}
_skill_view_tracker_lock = threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this conversation. "
    "The earlier skill_view result still carries the full body — use it. "
    "If that earlier result is now a [SKILL_PRUNED: ...] marker or a one-line "
    "summary, the body is gone and you must re-read it: call "
    "skill_view(name='<skill>', file_path='SKILL.md') to bypass this stub.")


def _skill_view_fingerprint(payload: dict) -> tuple | None:
    """Stat the skill file a successful skill_view served, for change detection."""
    if not (src := payload.get("_source_path")):
        return None
    try:
        st = os.stat(src)
        return (src, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _prune_destroyable_floor() -> int:
    """The compressor's skill-body prune floor, resolved at call time so the two constants
    cannot drift. ImportError (not a wide except) falls back to the safe direction: treat the
    body as destroyable, i.e. do NOT dedup it."""
    try:
        from agent.context_compressor import _SKILL_VIEW_PRUNE_MIN_CHARS
        return int(_SKILL_VIEW_PRUNE_MIN_CHARS)
    except ImportError:
        return 0


def _is_prune_destroyable(payload: dict) -> bool:
    """True when the served body is large enough that compaction may reduce it to a
    ``[SKILL_PRUNED: ...]`` marker — in which case a dedup stub would lock the model out of
    the reload that marker asks for, so the body must always be re-served."""
    content = payload.get("content")
    if not isinstance(content, str):
        return False  # no body served (e.g. a file listing): nothing to lose, dedup is safe
    return len(content) >= _prune_destroyable_floor()


def _record_skill_view(task_id, name, file_path, payload: dict) -> None:
    """Record a served skill_view so an identical repeat can be deduped."""
    # Never dedup setup-needed views: readiness depends on config/env state that
    # changes without the file changing; the model must see the refreshed status.
    if (not task_id or payload.get("setup_needed")
            or payload.get("readiness_status") == "setup_needed"):
        return
    if (fp := _skill_view_fingerprint(payload)) is None:
        return
    if _is_prune_destroyable(payload):
        return
    key = (str(payload.get("name") or name), file_path or "")
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        cache[key] = fp
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:  # FIFO eviction
            del cache[next(iter(cache))]


def _check_skill_view_dedup(task_id, name, file_path) -> str | None:
    """Dedup stub when this exact skill file was already served to this task and
    is unchanged on disk; None otherwise."""
    if not task_id:
        return None
    n = str(name)
    with _skill_view_tracker_lock:
        if not (cache := _skill_view_tracker.get(str(task_id))):
            return None
        # Record key is the RESOLVED name; match raw and resolved forms so
        # 'category/skill' and bare-name views coalesce.
        for key, (src, mtime_ns, size) in list(cache.items()):
            rec_name, rec_fp = key
            if rec_fp != (file_path or "") or (
                    rec_name != n and not n.endswith("/" + rec_name)
                    and not rec_name.endswith("/" + n) and n.split(":")[-1] != rec_name):
                continue
            try:
                st = os.stat(src)
                changed = (st.st_mtime_ns, st.st_size) != (mtime_ns, size)
            except OSError:
                changed = True
            if changed:
                cache.pop(key, None)
                return None
            return json.dumps({
                "success": True, "status": "unchanged", "name": rec_name,
                "file": file_path or "SKILL.md", "dedup": True, "content_returned": False,
                "message": _SKILL_VIEW_DEDUP_MESSAGE}, ensure_ascii=False)
    return None


def reset_skill_view_dedup(task_id: str | None = None) -> None:
    """Clear the dedup cache (all tasks when task_id is None); called on context compression."""
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
        else:
            _skill_view_tracker.pop(str(task_id), None)
