# Hermes Advisor Plans

**Audit date:** 2026-06-16
**Audited commit:** `130333a77` (local HEAD, v0.16.0)
**Upstream HEAD at audit time:** `c2c55c44`
**Gap:** 1,039 commits behind, 2 commits ahead (headroom lossless compression)

---

## Purpose

This directory holds plans for safely bringing curated upstream Hermes changes into a heavily-customized local checkout. The patterns are: (1) identify a small set of upstream commits we want, (2) cherry-pick them onto a new branch, (3) re-apply our customizations on top, (4) verify the result end-to-end, (5) keep a re-runnable update script for the next round.

## Plans in priority order

| # | Title | Effort | Risk | Status | Depends on |
|---|---|---|---|---|---|
| 001 | Integrate async background subagents from upstream Hermes | M (half-day) | MEDIUM | PROPOSED | — |
| 002 | Re-runnable Hermes update workflow (the answer to "updates always break") | S (one-time) | LOW | PROPOSED | 001 |

## Dependency graph

```
       ┌──────────────────────────────┐
       │ 001 async-subagent-integration │
       │   (cherry-pick 6 commits,     │
       │    create update script)      │
       └─────────────┬────────────────┘
                     │ creates tooling/update-hermes-with-customizations.sh
                     v
       ┌──────────────────────────────┐
       │ 002 hermes-update-workflow    │
       │   (operating model, docs,     │
       │    quarterly audit cron)      │
       └──────────────────────────────┘
```

## What the audit DID NOT cover

This is a **targeted plan** for the user's stated request (integrate async subagents + a safe update path). It is NOT a full security/performance/correctness audit. The following are explicitly out of scope:

- **Full upstream upgrade** of the other 1,033 commits. Each one is unvetted for compatibility with our headroom work.
- **Headroom customization reintegration** (the 1,620 lines of uncommitted changes plus 446 lines of carried commits, the 18 untracked files). The user already knows the headroom work is on the chopping block (per memory: "Headroom FULLY DISABLED" on June 14 2026) — that gets its own audit pass.
- **The carried headroom work in `gateway/run.py` (+7,931 lines).** Plan 001's only contact with this file is the +136-line `_async_delegation_watcher` addition, which the auditor verified should NOT textually conflict with our carried changes.
- **Any other feature gap** (kanban improvements, MCP server additions, etc.).

## Why a `cherry-pick` strategy and not `git pull --rebase`?

`git pull --rebase` would attempt to replay ALL 1,039 upstream commits on top of our 2 local commits. The 18 untracked headroom files and 7,931 lines of carried changes would conflict in dozens of files, and the resolution work would be unbounded.

Cherry-picking the curated 6-feature-commit set isolates the change to 8 files, of which 1 (`gateway/run.py`) has any meaningful conflict risk. The cherry-pick strategy is the difference between a half-day job and a multi-day audit.

## How to run the plans

For each plan in order:

1. Read the plan's "## Why this plan exists" section to understand the goal
2. Read "## Scope" to know what to touch
3. Execute "## Step-by-step procedure" in order
4. Run the "## Verification" section at the end
5. Report back if any step fails — do not improvise

The plans are designed for a less-capable executor. The auditor (Atlas) has already done the architectural reasoning; the executor's job is faithful execution.

## Future audits

When the user comes back for "another round" of plans, **reconcile, don't duplicate**:

1. Re-read this README and each plan's "## Status" line
2. Re-validate every assumption against current code
3. Look for what was missed: deployment topology (production-runtime recon), test coverage on the changed files, security implications of the new async code path
4. Add plans numbered 003+ rather than rewriting 001-002
5. Update this README with the new plans and the new dependency graph
