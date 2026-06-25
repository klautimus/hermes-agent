# Plan 002: Re-runnable Hermes update workflow (the answer to "updates always break")

**Status:** PROPOSED
**Author:** Atlas (improve skill, June 16 2026)
**Companion to:** plan 001 (async-subagent integration)
**Effort:** S (one-time script + docs)

---

## Why this plan exists

The user's stated problem: "we have so many customizations to the Hermes code that it always breaks on updates." This is true today, and plan 001 alone doesn't fix it — it only solves the immediate async-subagent need.

The longer-term fix is a **re-runnable update workflow** that:

1. Cherry-picks the curated set of upstream features we want (just plan 001's 6 commits, for now)
2. Verifies the cherry-picks still apply on top of a new upstream SHA before doing anything destructive
3. Re-applies our local customizations in the correct order relative to the cherry-picks
4. Surfaces a single CLI invocation for the user, not a 5-step ritual

This plan builds that workflow. Plan 001 creates the script (`tooling/update-hermes-with-customizations.sh`); this plan codifies the **operating model** around it so the next time upstream ships something, the user has a one-command path.

## The operating model

### The four layers

```
Layer 1: Upstream Hermes (c2c55c44, ~3,000 commits, the source of truth)
  |
  v  cherry-pick (FEATURE_COMMITS, 6 commits as of plan 001)
  |
Layer 2: integration/async-subagents branch (this plan's deliverable)
  |
  v  rebase on top of latest local main (when local main moves)
  |
Layer 3: main (local, with headroom customizations + 2 local commits)
  |
  v  (today: nothing — this layer is hand-managed)
  |
Layer 4: deployed ~/.hermes/hermes-agent/ (a working copy at the user's $HERMES_HOME)
```

The user wants Layer 4 to "just work." The path from Layer 1 to Layer 4 is:

```
hermes update
   -> pull upstream main to Layer 1 (1039+ commits)
   -> this script: cherry-pick curated features onto Layer 1
   -> rebase Layer 2 onto Layer 3 (merge with our customizations)
   -> rebuild venv (pip install -e .)
   -> restart gateway
```

### Today's state vs. the target

**Today (June 16 2026):** The user has been doing this by hand — pull upstream, hit conflicts, manually re-apply the headroom work, ship. The 1039-commit gap is the cumulative cost.

**Target:** `./tooling/update-hermes-with-customizations.sh <upstream_sha>` is a one-command operation. The script's job is to make the boring version of the update workflow work, and to surface the hard cases (real conflicts) clearly.

## Scope

### Files in scope

- `tooling/update-hermes-with-customizations.sh` — created by plan 001 Step 6 (this plan formalizes it)
- `advisor-plans/002-hermes-update-workflow.md` — this document
- `advisor-plans/README.md` — the index for both plans (created separately)
- `~/.hermes/scripts/hermes-update-hook.sh` — OPTIONAL: a post-`hermes update` hook that re-applies customizations. Skip if `hermes update` doesn't support hooks.

### Files explicitly OUT of scope

- Any Hermes source file (this plan doesn't write Hermes code, it writes the workflow around it)
- The headroom customizations themselves (those live in plan 002's sibling, the unaddressed-but-implied plan 003 for headroom reintegration)
- CI/CD or packaging changes (Hermes isn't a project the user ships; it's a runtime they deploy)

## Step-by-step procedure

### Step 1: Verify plan 001 Step 6's script is in place

```bash
cd /home/kevin/.hermes/hermes-agent
test -x tooling/update-hermes-with-customizations.sh && echo "PASS" || echo "FAIL: run plan 001 Step 6 first"
```

The script from plan 001 already does the heavy lifting. This plan is the "operating model + docs + future-evolution" wrapper around it.

### Step 2: Document the workflow in a user-facing guide

Create `advisor-plans/HERMES_UPDATE_GUIDE.md` (sibling to this plan) with:

1. **One-paragraph summary** of why updates break, and what this workflow does about it
2. **The 4-layer model diagram** (above)
3. **A worked example:** "Updating from v0.16.0 to v0.17.0"
4. **The recovery procedure:** if the script reports a conflict on commit X, what to do
5. **The CI parity note:** "This workflow mirrors what `hermes update` would do if it supported cherry-pick. Until then, the script is the path."

The guide should be ~150 lines, written in plain English, link out to the script and the cherry-pick set, and be readable by Kevin 6 months from now without re-reading the conversation.

### Step 3: Add a `make update` shortcut (optional but recommended)

If the project has a `Makefile`, add:

```makefile
update: ## Pull upstream Hermes, port customizations, re-run tests
	./tooling/update-hermes-with-customizations.sh origin/main
```

If the project doesn't have a `Makefile`, skip — the script is the source of truth.

### Step 4: Schedule a quarterly upstream-gap audit

Add a single cron entry that reminds the user to look at upstream every 90 days:

```bash
hermes cron create 0 9 1 */3 * \
    --name hermes-upstream-gap-audit \
    --deliver origin \
    --prompt "$(cat <<'EOF'
Run /home/kevin/.hermes/hermes-agent/tooling/update-hermes-with-customizations.sh origin/main
in dry-run mode (just verify, don't apply). Report:
- Total commits behind upstream
- How many of our FEATURE_COMMITS are still in the new HEAD
- Any commits with conflicts (the script will list them)
- Recommendation: clean update, defer, or escalate
EOF
)"
```

Don't actually run `hermes cron create` here — that's a side-effect that requires user approval per the `hermes cron create` positional-`prompt` bug workaround. Surface the proposed cron entry to the user and let them fire it themselves with:

```bash
hermes cron create "0 9 1 */3 *" --name hermes-upstream-gap-audit --no-agent --script /home/kevin/.hermes/hermes-agent/tooling/quarterly-upstream-audit.sh
```

…after writing the audit script that the cron entry calls.

### Step 5: Add the new commits to FEATURE_COMMITS when upstream ships the next feature

The script's `FEATURE_COMMITS` array is the curated list of upstream commits we've decided to port. Every time upstream ships a feature the user wants:

1. Identify the feature commits (one or more `git log --grep="feat(...)"` runs on upstream main)
2. Add the SHAs in chronological order to `FEATURE_COMMITS` in the script
3. Bump the script's version comment (top of file, `v0.1.0` -> `v0.2.0` etc.)
4. Commit the change
5. Re-run the script to verify all the old cherry-picks still apply on top of the new base

The 1039-commit gap as of this plan is a *backlog*, not a permanent state. The script's job is to keep the gap from growing past "what the user actively wants."

## Verification

After completing this plan:

1. `cat advisor-plans/HERMES_UPDATE_GUIDE.md` should produce a readable 150-line document.
2. `./tooling/update-hermes-with-customizations.sh` should print the cherry-pick commands and exit 0.
3. The proposed cron entry from Step 4 should be visible to the user (just *proposed*, not fired).

## Maintenance notes

- The script is the canonical source of the FEATURE_COMMITS list. If you find yourself adding cherry-picks to a separate file, you're duplicating state.
- When `hermes update` gains cherry-pick support, retire this script (or turn it into a thin wrapper around `hermes update --cherry-pick FEATURE_COMMITS`).
- The quarterly audit cron is a *proactive* measure. If the user starts updating weekly, retire the quarterly cron.

## Escape hatches

- **If the user wants the full 1,039-commit update:** this plan does NOT support that. The 1,033 non-feature upstream commits have not been audited for compatibility with our headroom work. Escalate to a fresh audit pass (run `improve` with `focus=tech-debt` on the gap) before attempting.
- **If the script keeps failing on the same commit across multiple upstream releases:** that commit is structurally incompatible with our customizations. Open a follow-up plan to either (a) find a different way to express the feature, or (b) deprecate the customization.

---

**Plan version:** 1.0
**Filed:** 2026-06-16
**Auditor:** Atlas
