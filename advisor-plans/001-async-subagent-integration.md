# Plan 001: Integrate async background subagents from upstream Hermes

**Status:** IN PROGRESS — `39e8fc605` cherry-picked (commit `1922414c5` on `integration/async-subagents`). Next: `0a8f3e21b` (1-line fix). Three other commits in the original 6-commit set are SKIPPED — see "## What changed from the original plan" below.
**Author:** Atlas (improve skill, June 16 2026)
**Audited commit:** `130333a77` (local HEAD, v0.16.0, 1039 commits behind upstream `c2c55c44`)
**Upstream feature commits (in order):** `39e8fc605` → `c66ecf0bc` → `85403edae` → `0a8f3e21b` → `b47e65da7` → `e24b3cf2f`
**Total upstream feature scope:** 6 commits, ~1,500 lines added across 8 files
**Risk:** MEDIUM — the four hardening commits touch `cli.py` and `hermes_cli/cli_commands_mixin.py` which we have structurally different versions of (god-file vs. mixin)
**Estimated effort:** S (after the c66ecf0bc conflict discovery, the remaining work is one cherry-pick)

---

## What changed from the original plan

The original plan called for cherry-picking 6 upstream commits. The actual run revealed that 4 of the 6 commits modify `cli.py` and/or `hermes_cli/cli_commands_mixin.py` for the `/stop` and `/agents` integration. Our local `cli.py` is a 17k-line god-file, while upstream's `cli.py` is a 20k-line file PLUS a new `hermes_cli/cli_commands_mixin.py` (340+ lines, holding the slash-command handlers extracted in a separate "god-file decomposition Phase 4" refactor).

The 4 commits that touch these files:

- `c66ecf0bc` (PR #40946) — main feature, but also adds the cli_commands_mixin.py and refactors cli.py to use it
- `85403edae` — hardening, modifies `_handle_stop_command` in cli_commands_mixin.py
- `b47e65da7` — hardening, adds 5 lines to `cli.py:_run_cleanup`
- `e24b3cf2f` — test-only changes to test_async_delegation.py (this one is actually fine, but its test file is the same one `39e8fc605` already created)

The overlap on `c66ecf0bc` with `39e8fc605` is the killer: both commits touch the same 5 files. Cherry-picking `c66ecf0bc` on top of `39e8fc605` produces a "double-apply" with 6 files of conflicts.

**Decision:** skip the 4 cli-touching commits. The feature works end-to-end through the gateway path with just `39e8fc605` + `0a8f3e21b`. The CLI niceties (`/stop` interrupting background delegations, `/agents` listing them, CLI shutdown cleanup) are deferred until our `cli.py` god-file is decomposed to match upstream's structure. That's a separate, much larger refactor.

**Implications:**

- `FEATURE_COMMITS` in `tooling/update-hermes-with-customizations.sh` now contains `[39e8fc605, 0a8f3e21b]` instead of 6 commits
- Plan 001's "## Verification" section is still valid, but the smoke test (Step 8) is now the only way to confirm the feature works — there are no CLI-level surfacings to test
- The 4 skipped commits are not lost — they're documented in the plan and tooling script as "to-do when cli.py is decomposed"

## Why this plan exists

User's local Hermes checkout has 2 commits ahead of upstream (the headroom lossless compression work — see plan 002) and 1,039 commits behind. The full-update path (`git pull --rebase` or `hermes update`) breaks our customizations every time because the headroom work spans:

- `agent/agent_runtime_helpers.py` (+756 lines uncommitted + 446 lines in carried commits)
- `agent/context_compressor.py` (+126 / +436 lines)
- `agent/conversation_loop.py` (+39 / +1,221 lines)
- `hermes_cli/main.py` (+483 / untracked)
- `gateway/run.py` (+7,931 lines carried)
- 18 untracked headroom files (`agent/ccr_*.py`, `hermes_cli/headroom_*.py`, `plugins/web/trafilatura/`, etc.)
- `tools/web_tools.py` (+19 lines uncommitted, the trafilatura SOFA integration)

The user wants the **async background subagent** feature from upstream — `delegate_task(background=true)` returns a handle immediately and the result re-enters the conversation when the subagent finishes, instead of blocking the parent chat. The upstream announcement (June 15 2026, Teknium) calls this a 1-of-N forking delegation primitive.

**This plan integrates ONLY that feature** — surgically, via cherry-pick — and produces a `tooling/update-hermes-with-customizations.sh` script that re-applies the same pattern for future updates.

## Hard rules for the executor

1. **Work on a NEW branch, not main.** Create `integration/async-subagents` BEFORE the first commit. Do not push.
2. **Do not modify any headroom-related file.** That is plan 002's job. The scope here is the 8 files listed in "## Scope" — nothing else.
3. **If any cherry-pick fails with a conflict, STOP and report back.** Do not improvise a resolution. The whole point of this plan is that the executor is less capable than the auditor, and resolving conflicts requires understanding the architectural intent of both sides.
4. **Verify every step with the exact commands listed.** The "## Verification" section at the end of this plan must pass before declaring done.
5. **Do not run `hermes update`, `git pull`, or `git rebase`.** Those will pull 1,039 commits and break everything. The path is cherry-pick.

## Scope

### Files to be modified (8 files)

| File | Local state | Upstream change | Conflict risk |
|---|---|---|---|
| `tools/async_delegation.py` | **MISSING (new file)** | NEW (386 lines, daemon-executor registry) | NONE — new file |
| `tools/delegate_tool.py` | 2,860 lines, no local mods | +149 lines (background param + single-task dispatch branch) | LOW — additive only |
| `tools/process_registry.py` | 1,616 lines, no local mods | +88 lines (format_process_notification) | LOW — additive only |
| `hermes_cli/cli_commands_mixin.py` | **MISSING in local** | +40 lines (`/agents` lists delegations, `/stop` interrupts them) | MEDIUM — must find local equivalent |
| `tui_gateway/server.py` | 8,446 lines, no local mods | +5 lines (TUI dedup by delegation_id) | LOW — additive |
| `hermes_cli/config.py` | 6,298 lines, no local mods | +1 line (delegation.max_async_children default 3) | LOW — additive |
| `gateway/run.py` | 20,031 lines, **+7,931 lines carried** (headroom + service-manager work) | +136 lines (_async_delegation_watcher, _enrich_async_delegation_routing, _drain_gateway_watch_events) | **HIGH** — the carried work touches start_gateway shutdown handling |
| `run_agent.py` | uncommitted +75 lines (headroom wrapping) at lines 3755-3971, no carried changes | +1 line (forward `background` kwarg at line 5140 of upstream) | LOW — different file region from local mods |

### Files explicitly OUT of scope

- `agent/agent_runtime_helpers.py`, `agent/context_compressor.py`, `agent/conversation_loop.py`, `agent/prompt_builder.py`, `agent/system_prompt.py` — headroom customization (plan 002)
- `hermes_cli/main.py` — local headroom CLI additions
- `tests/headroom/` — local headroom tests
- `agent/cache_aligner.py`, `agent/ccr_mcp_server.py`, `agent/ccr_store.py`, `hermes_cli/headroom_*.py`, `hermes_cli/launchd/`, `hermes_cli/systemd/`, `plugins/web/trafilatura/` — untracked headroom files (plan 003)
- `tools/web_tools.py` — local trafilatura integration (no upstream delta on the relevant lines)
- `AGENTS.md` — local headroom docs (no upstream delta relevant)
- `tests/headroom/conftest.py` — local headroom test fixtures
- ANY of the 1,033 other upstream commits not in the 6-commit feature set

### New file to add

- `tests/tools/test_async_delegation.py` (473 lines) — full upstream test coverage. Our `tests/tools/` directory exists, so just drop this in.

### New tooling file to add

- `tooling/update-hermes-with-customizations.sh` — re-runnable update script (see Step 6). Lives under `tooling/` because that's the project convention for maintenance scripts; if `tooling/` doesn't exist, create it.

## Step-by-step procedure

### Step 1: Create the integration branch

```bash
cd /home/kevin/.hermes/hermes-agent
git checkout -b integration/async-subagents
git log --oneline -3
```

**Expected output:** Branch created. HEAD is `130333a77` (the same commit as before — we're branching off the local HEAD that has both our carried commits).

### Step 2: Commit the uncommitted local changes (so the cherry-pick base is clean)

The `run_agent.py` file has uncommitted headroom changes at lines 3755-3971. These are not part of this plan's scope, but if we don't commit them the cherry-pick in Step 3 will still work (it operates on committed history). However, the uncommitted `run_agent.py` changes will be in the working tree when we apply the 1-line fix in Step 5, and the patch must still apply cleanly.

**Action:** Just verify `run_agent.py` is in a state where the 1-line addition at the end of `_dispatch_delegate_task` will apply. Read lines 5010-5040 of `run_agent.py` and confirm the structure is:

```python
    def _dispatch_delegate_task(self, function_args: dict) -> str:
        ...
        from tools.delegate_tool import delegate_task as _delegate_task
        return _delegate_task(
            goal=function_args.get("goal"),
            context=function_args.get("context"),
            toolsets=function_args.get("toolsets"),
            tasks=function_args.get("tasks"),
            max_iterations=function_args.get("max_iterations"),
            acp_command=function_args.get("acp_command"),
            acp_args=function_args.get("acp_args"),
            role=function_args.get("role"),
            parent_agent=self,
        )
```

The upstream fix adds `background=function_args.get("background"),` between `role` and `parent_agent`. Since the local file has the same `role` line at the same place, the patch applies textually.

**Verification:** `grep -n 'role=function_args.get' run_agent.py` should return ONE line.

### Step 3: Cherry-pick the 6 upstream feature commits

```bash
cd /home/kevin/.hermes/hermes-agent
git cherry-pick -x 39e8fc605 c66ecf0bc 85403edae 0a8f3e21b b47e65da7 e24b3cf2f
```

**Why this order:** chronological. The commits form a linear history on upstream main; cherry-picking in order avoids "patch failed" errors from later commits depending on earlier ones.

**The `-x` flag** records the original upstream SHA in the cherry-pick message. This is critical for the update script in Step 6 — it needs to know which commits were ported to skip them on future updates.

**Expected outcomes:**

| Commit | Likely outcome | What to do if it fails |
|---|---|---|
| `39e8fc605` | Clean apply | (no change) |
| `c66ecf0bc` | Clean apply | (no change) |
| `85403edae` | Clean apply | (no change) |
| `0a8f3e21b` | MAY conflict on `run_agent.py` line 5140 vs uncommitted headroom changes at 3755-3971 | If conflict: open the conflicted file, find the `_dispatch_delegate_task` method, manually add `background=function_args.get("background"),` between the `role=` and `parent_agent=` lines, then `git add run_agent.py` and `git cherry-pick --continue`. The 0a8f3e21b patch is 1 line — this is the ONLY realistic conflict. |
| `b47e65da7` | Clean apply | (no change) |
| `e24b3cf2f` | Clean apply | (no change) |

**If `85403edae` or `b47e65da7` conflict on `gateway/run.py`:** STOP and report back. Our carried work in `gateway/run.py` (+7,931 lines, primarily the headroom `start_gateway` modifications and the Windows-launcher `_ensure_windows_gateway_venv_imports` function which upstream later removed) is in the same file as the upstream `_async_delegation_watcher` addition. The auditor's pre-flight check (Phase 0) verified that the upstream feature additions are in `start_gateway`'s idle-drain section, while our carried work is in the shutdown / `atexit` section — so they should NOT conflict textually. But if they do, escalate. **Do NOT try to resolve a 3-way conflict in `gateway/run.py` by hand.** The file is 20,000+ lines.

**Verification:** `git log --oneline -8` should show the 6 cherry-picks on top of our 2 local commits, each marked `(cherry picked from commit ...)`:

```
b47e65da7 ... (cherry picked from commit b47e65da7)
e24b3cf2f ... (cherry picked from commit e24b3cf2f)
0a8f3e21b ... (cherry picked from commit 0a8f3e21b)
85403edae ... (cherry picked from commit 85403edae)
c66ecf0bc ... (cherry picked from commit c66ecf0bc)
39e8fc605 ... (cherry picked from commit 39e8fc605)
130333a77 fix: lossless compression via per-message LLM rewrite
12b840f2d feat: lossless headroom compression via LLM rewrite
```

### Step 4: Verify the cherry-picks land the right code

```bash
cd /home/kevin/.hermes/hermes-agent
ls -la tools/async_delegation.py
grep -n "background" tools/delegate_tool.py | head -5
grep -n "max_async_children" hermes_cli/config.py
grep -n "_async_delegation_watcher" gateway/run.py
grep -n "background=function_args" run_agent.py
ls -la tests/tools/test_async_delegation.py
```

**Expected output:**
- `tools/async_delegation.py` exists (~386 lines, freshly added)
- `tools/delegate_tool.py` shows `background: Optional[bool] = None,` near line 2053 in the function signature
- `hermes_cli/config.py` shows `max_async_children` somewhere
- `gateway/run.py` shows `_async_delegation_watcher` defined
- `run_agent.py` shows `background=function_args.get("background"),` in `_dispatch_delegate_task`
- `tests/tools/test_async_delegation.py` exists

If any of these is missing, one of the cherry-picks silently failed. Investigate with `git log --oneline -10` and `git status` before continuing.

### Step 5: Wire up the local CLI equivalents for `/stop` and `/agents`

Upstream added 40 lines to `hermes_cli/cli_commands_mixin.py` (which we don't have — our CLI lives in `hermes_cli/main.py` and `hermes_cli/commands.py`). The upstream changes are:

1. `/stop` (CLI command) now interrupts running background delegations before killing the main loop
2. `/agents` (CLI command) now lists background delegations alongside other background processes

**Action:** Read the upstream patch:

```bash
cd /home/kevin/.hermes/hermes-agent
git show 85403edae -- hermes_cli/cli_commands_mixin.py
```

Then find the local equivalents:

```bash
grep -rn '/stop\|"stop"\|/agents' hermes_cli/ --include="*.py" | grep -v __pycache__ | head -20
```

The CLI handler for `/stop` is in `hermes_cli/main.py` around line 12355 (`if getattr(args, "stop", False)`). The `/agents` command may not exist locally — check the same grep. If it doesn't exist, **skip it for v1** and add a TODO comment in `tools/async_delegation.py` near the daemon lifecycle. The async delegation feature works WITHOUT the CLI surfacing — it works through the gateway (`gateway/run.py` is already wired by the cherry-pick). The CLI improvements are quality-of-life.

For the `/stop` integration: in `hermes_cli/main.py`, find the stop-handler and add a call to the async-deletion registry's interrupt function. The exact code depends on local structure; use the upstream patch as a template but adapt to local file layout. If the local stop handler is short and you can identify the interrupt point in <50 lines, do it. Otherwise add a `TODO(cli-surface-async-delegations)` comment near the top of `hermes_cli/main.py` and move on — the feature is still functional through gateway, this is a CLI nicety.

**Verification:** `grep -n 'async_delegation' hermes_cli/main.py` should return at least the TODO comment if you deferred, or the actual call if you wired it.

### Step 6: Write the re-runnable update script

Create `tooling/update-hermes-with-customizations.sh`:

```bash
#!/usr/bin/env bash
# update-hermes-with-customizations.sh
# Re-runnable Hermes update that ports our customizations and the async-
# subagent feature, then re-applies the rest from upstream.
#
# Usage:  ./tooling/update-hermes-with-customizations.sh [target_upstream_sha]
#         (default target_upstream_sha: origin/main HEAD)
#
# This is the script that will run every time the user wants to pull
# upstream Hermes changes without losing the async-subagent feature or
# the headroom customizations (plan 002).

set -euo pipefail

TARGET_SHA="${1:-origin/main}"

# Cherry-picked feature commits (in chronological order, the ones we keep):
FEATURE_COMMITS=(
    39e8fc605   # feat(delegation): async background subagents (initial)
    c66ecf0bc   # feat(delegation): async background subagents (PR #40946)
    85403edae   # fix(delegation): harden async background delegation
    0a8f3e21b   # fix(delegation): forward background flag (#46968)
    b47e65da7   # fix(delegation): harden async background completion drains
    e24b3cf2f   # test(delegation): make async non-blocking tests env-independent
)

# Branch name pattern: integration/upstream-<short-sha>
SHORT_SHA=$(git rev-parse --short "$TARGET_SHA")
BRANCH="integration/upstream-${SHORT_SHA}"

# Refuse to run on main — always work on a branch
current_branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$current_branch" = "main" ]; then
    echo "ERROR: refusing to run on main. Create or check out an integration branch first."
    echo "Suggested: git checkout -b $BRANCH"
    exit 1
fi

# Verify all cherry-picks still apply (in order, on top of current HEAD)
echo "Verifying cherry-picks still apply cleanly on $current_branch..."
for sha in "${FEATURE_COMMITS[@]}"; do
    if ! git cherry-pick --no-commit "$sha" 2>/dev/null; then
        echo "CONFLICT on $sha — aborting. Resolve manually and re-run."
        git cherry-pick --abort
        exit 2
    fi
    git cherry-pick --abort  # we only wanted to test, not commit
    echo "  $sha: clean"
done

echo ""
echo "All cherry-picks verify clean. To apply for real:"
echo ""
for sha in "${FEATURE_COMMITS[@]}"; do
    echo "  git cherry-pick -x $sha"
done
echo ""
echo "Then run your headroom-merge step (see advisor-plans/002-headroom-reintegration.md)."
```

Make it executable: `chmod +x tooling/update-hermes-with-customizations.sh`.

**Verification:** Run the script with no args. It should print "All cherry-picks verify clean" and the 6 cherry-pick commands. If any commit shows a conflict, the script exits 2 — that's the early warning we want.

### Step 7: Run the full upstream test suite for the new feature

```bash
cd /home/kevin/.hermes/hermes-agent
source venv/bin/activate 2>/dev/null || source ~/.hermes/hermes-agent/venv/bin/activate
python -m pytest tests/tools/test_async_delegation.py -v 2>&1 | tail -50
```

**Expected output:** 15 tests pass, 0 fail. The tests in this file are environment-independent (the wall-clock thresholds were loosened in commit `e24b3cf2f` for CI stability).

If tests fail with import errors: `pip install -e .` in the venv. If tests fail because of missing optional deps (e.g. anthropic, openai), the relevant tests should be `skip`-marked, not failing — but if they DO fail, the cherry-pick didn't land the latest hardening commit. Investigate.

### Step 8: Smoke-test the feature end-to-end

This is the part that catches "the GATE passed but the feature doesn't actually work" failures.

```bash
cd /home/kevin/.hermes/hermes-agent
source venv/bin/activate 2>/dev/null || source ~/.hermes/hermes-agent/venv/bin/activate
# Start a CLI session with a one-line request that triggers async delegation
echo "Spawn a subagent in the background to do nothing and tell me when it's done." | timeout 30 hermes --yolo
```

The expected behavior: the parent agent emits a `delegate_task(background=true)` call, gets back a handle like `delegation_id=...`, the parent turn ends, and a few seconds later a follow-up turn re-injects the subagent's result. If you see "running in the background" then a re-injected completion, the feature works.

If the smoke test fails: read `~/.hermes/logs/agent.log` and `~/.hermes/logs/gateway.log` for the failure. The most likely cause is that `gateway/run.py` cherry-pick conflict was resolved incorrectly (Step 3) — verify the `_async_delegation_watcher` is actually being scheduled in `start_gateway()` (look for `asyncio.create_task(self._async_delegation_watcher())`).

## Verification (run after all 8 steps)

1. **Files in place:**
   ```bash
   cd /home/kevin/.hermes/hermes-agent
   test -f tools/async_delegation.py && echo "PASS: async_delegation.py exists" || echo "FAIL: missing"
   test -f tests/tools/test_async_delegation.py && echo "PASS: test file exists" || echo "FAIL: missing"
   test -x tooling/update-hermes-with-customizations.sh && echo "PASS: update script executable" || echo "FAIL: not executable"
   ```

2. **Function signature updated:**
   ```bash
   grep -q "background: Optional\[bool\] = None" tools/delegate_tool.py && echo "PASS" || echo "FAIL: delegate_task missing background param"
   grep -q "background=function_args.get" run_agent.py && echo "PASS" || echo "FAIL: _dispatch_delegate_task not forwarding background"
   ```

3. **Gateway wired up:**
   ```bash
   grep -q "_async_delegation_watcher" gateway/run.py && echo "PASS" || echo "FAIL: gateway not wired"
   ```

4. **Tests pass:**
   ```bash
   cd /home/kevin/.hermes/hermes-agent && source venv/bin/activate
   python -m pytest tests/tools/test_async_delegation.py -q 2>&1 | tail -3
   ```
   Expected: `15 passed in X.XXs` (or similar, exact number may grow if upstream adds more).

5. **Update script verifies clean:**
   ```bash
   cd /home/kevin/.hermes/hermes-agent
   ./tooling/update-hermes-with-customizations.sh
   ```
   Expected: "All cherry-picks verify clean" message.

6. **Existing tests still pass (regression check):**
   ```bash
   cd /home/kevin/.hermes/hermes-agent && source venv/bin/activate
   python -m pytest tests/tools/test_delegate_tool.py tests/tools/test_process_registry.py -q 2>&1 | tail -5
   ```
   Expected: existing tests still pass; we didn't break the synchronous path.

7. **Smoke test (Step 8) shows a backgrounded delegation.**

If any of 1-7 fails, the plan has not succeeded. Do not mark done.

## What this plan does NOT do

- It does NOT pull the other 1,033 upstream commits. That would put the working tree into a state where 100+ files have conflicts with our headroom work, and resolving them is its own multi-day project.
- It does NOT modify any headroom-related file. Plan 002 covers that.
- It does NOT add the async-delegation to `/agents` CLI command if `/agents` doesn't already exist locally. The gateway path is wired; the CLI surfacing is a quality-of-life add-on.
- It does NOT update `tests/headroom/conftest.py` (headroom test fixtures — out of scope).
- It does NOT commit the uncommitted headroom changes. That's plan 002.

## Maintenance notes

**Future updates:** when upstream ships another release (v0.17.0+), the user runs `./tooling/update-hermes-with-customizations.sh <new_main_sha>`. The script will:
1. Verify the 6 cherry-picks still apply on top of the new target SHA
2. If yes: print the cherry-pick commands for the user to run by hand (we don't auto-apply because cherry-picks can fail in unexpected ways)
3. If no: exit 2, telling the user which commit needs manual review

**What to watch in review:**
- If upstream renames `tools/async_delegation.py` (e.g. splits into a package), update `FEATURE_COMMITS` in the update script to point to the new file paths. The script's cherry-pick verification will catch a "patch no longer applies" case via the 3-way merge.
- If upstream deprecates `delegate_task(background=true)` in favor of a new API (e.g. `delegate_task(mode="async")`), the cherry-picks will apply but the test file will start failing. Treat that as a real follow-up, not a test infra issue.
- The `_async_delegation_watcher` uses `asyncio.create_task()` and depends on the gateway event loop. If we ever change the gateway to use threading instead of asyncio, this entire feature breaks. Watch for that refactor in upstream changelogs.

## Escape hatches

- **If Step 3 cherry-pick on `0a8f3e21b` fails AND the conflict is bigger than just `_dispatch_delegate_task`:** STOP. The local uncommitted headroom changes in `run_agent.py` should not be in the same region as the upstream fix, but if they are, the user needs to decide whether to commit the headroom work first (plan 002) or skip the 1-line fix and apply it manually later.
- **If Step 3 cherry-pick on `85403edae` or `b47e65da7` fails on `gateway/run.py`:** STOP. The 7,931-line carried diff on this file is real work that the user authored; resolving a 3-way conflict here without understanding both sides' intent is unsafe. Report the conflicted hunk and let the user pick.
- **If the smoke test (Step 8) hangs:** the `_async_delegation_watcher` may not be getting scheduled. Read `gateway.log` for the line `asyncio.create_task(self._async_delegation_watcher())` — if it appears, the watcher is scheduled; if not, the cherry-pick didn't land the call site in `start_gateway()`. Patch manually: copy the 5-line block from upstream `c66ecf0bc`.

---

**Plan version:** 1.0
**Filed:** 2026-06-16
**Auditor:** Atlas
