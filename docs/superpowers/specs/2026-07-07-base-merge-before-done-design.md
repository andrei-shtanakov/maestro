# Base merge before DONE — design

**Date:** 2026-07-07
**Status:** approved
**Context:** C4 follow-up #2 (from the orchestrator startup-recovery final review,
PR #48). Recorded in TODO.

## Problem

In `Orchestrator._handle_success`, `_merge_into_base` (merge the feature branch
into the base branch of the main repo) runs AFTER the `DONE` transition:

```
RUNNING → MERGING → [PR create → PR_CREATED] → DONE → _merge_into_base → cleanup
```

Two defects follow from this ordering:

1. **Crash during the base merge → unrecoverable `DONE`.** A hard crash (SIGKILL,
   power loss) while `_merge_into_base` runs leaves the workstream showing `DONE`
   with its feature branch never merged into base. Startup recovery skips terminal
   `DONE`, so the work silently never lands in base. This is the exact
   "DONE but not actually merged" state the just-shipped startup recovery could
   NOT catch (noted as the out-of-scope gap in that PR).

2. **A merge CONFLICT is silently swallowed.** `_merge_into_base` today is a raw
   `subprocess.run` that, on a non-zero return, only logs a warning — no
   `git merge --abort`, no raised error. So a conflict leaves the base repo in a
   half-merged / conflicted state AND the workstream still proceeds to `DONE`.

The reordering fix for (1) forces confronting (2): moving the merge before `DONE`
is only meaningful if `DONE` is gated on merge success. Both are addressed here.

## Change

Move the base merge before the `DONE` transition and gate `DONE` on its success;
harden `_merge_into_base` to abort a failed merge and raise a typed error; route a
merge failure to `NEEDS_REVIEW` (a conflict needs human resolution — re-running
`run --all` cannot fix it).

New ordering in `_handle_success`:

```
RUNNING → MERGING → [PR create → PR_CREATED] → base merge
    ├─ success → PR_CREATED → DONE, stats.completed++, cleanup workspace
    └─ failure → PR_CREATED → FAILED → NEEDS_REVIEW, stats.failed++, NO cleanup
```

### `_handle_success` restructure

Both `auto_pr` paths already converge at `PR_CREATED` before the final `DONE`
write (auto_pr=True sets it in the PR block; auto_pr=False passes MERGING through
PR_CREATED). So:

1. Keep the existing MERGING and PR-create logic unchanged up to the point where
   status is `PR_CREATED` (auto_pr=True) or `MERGING` (auto_pr=False).
2. If still `MERGING` (auto_pr=False), transition `MERGING → PR_CREATED` (the
   existing pass-through, moved up).
3. **At `PR_CREATED`, attempt the base merge** (`run_in_executor`, as today):
   - **Success:** `PR_CREATED → DONE` (`expected_status=PR_CREATED`),
     `stats.completed += 1`, `cleanup_workspace`.
   - **Failure (`GitError`):** log; `PR_CREATED → FAILED`
     (`error_message` = the merge error); `FAILED → NEEDS_REVIEW`
     (`expected_status=FAILED`); `stats.failed += 1`; **return without cleanup**
     (leave the worktree so a human has full context to resolve the conflict; the
     feature branch persists in the main repo's refs regardless).

`PR_CREATED → NEEDS_REVIEW` is not a valid transition (`PR_CREATED → {DONE,
FAILED}`), so the failure path routes through `FAILED` — both writes are valid
(`FAILED → NEEDS_REVIEW`). Writing `error_message` here IS wanted (a genuine
failure the human must see), unlike the pure-reset recovery path.

Do NOT reuse `_handle_failure` for the merge failure: it applies retry accounting
(`can_retry → READY` → re-run `run --all`), which cannot resolve a merge conflict
and would just burn retries before landing in `NEEDS_REVIEW` anyway. Route to
`NEEDS_REVIEW` directly.

The merge is `await`ed via `run_in_executor`, so a raised `GitError` re-raises at
the await and is caught by the `try/except` in `_handle_success` — the exception
no longer escapes unhandled into `_monitor_running`.

### `_merge_into_base` hardening

Keep the existing signature, the `span("task.execute", …)` and `child_env()` obs
wrapping, and the raw-subprocess mechanics (preserves trace propagation into the
git subprocess). Change only the failure handling:

- Before merging, verify the main repo is on `base_branch` (see "Verify the
  base-branch invariant" below); raise `GitError` if not.
- On `returncode == 0`: log success, return (unchanged).
- On `returncode != 0`: run `git merge --abort` (`check=False`, best-effort — it
  cleans a conflicted / partial-merge state so the base repo is left clean) and
  then raise — `MergeConflictError` if the stderr indicates a conflict
  (`"conflict" in stderr.lower()`), else `GitError`. Reuse these error types from
  `maestro.git` so the exception taxonomy is shared (`MergeConflictError` and
  `BranchNotFoundError` are both subclasses of `GitError`, so `_handle_success`
  catches `GitError`).

**Verify the base-branch invariant before merging (do not `checkout`).** In the
Mode-2 worktree topology the main repo (`repo_path`) stays on `base_branch`
throughout — workstreams run in separate worktrees on feature branches — so
`git merge feature` in `repo_path` merges into base. But this invariant is
currently unchecked: if the main repo is on the wrong branch (or detached), the
raw merge would silently land the feature branch into the wrong place — and on the
crash-recovery re-run path that looks like a *successful* recovery while the work
goes to the wrong branch. So before merging, `_merge_into_base` READS the current
branch (`git rev-parse --abbrev-ref HEAD`) and, if it is not `base_branch` (this
also catches a detached HEAD, which returns `"HEAD"`), raises `GitError` without
attempting the merge → routed to `NEEDS_REVIEW`. This converts a silent
wrong-branch merge into a loud, safe failure.

A read-only *verify* is chosen over an active `checkout base`: verify adds no new
failure surface (it never touches the working tree) and refuses loudly rather than
silently "correcting" a state the operator may have set deliberately. Not reusing
`GitManager.merge_branch` (which checks out target and would need a
verify-repo-at-init `GitManager` instance injected onto the hot completion path) —
hardening in place keeps the obs wrapping and the change surgical.

### Recovery interaction

The just-shipped `_recover_stranded_workstreams` (PR #48) is what makes the
reordering recoverable:

- **Crash BETWEEN the merge commit and the `DONE` write** → workstream is in
  `PR_CREATED` (pre-DONE) → startup recovery resets it to `READY` → re-run →
  `_handle_success` re-attempts the merge → `git merge feature` reports
  "Already up to date" (`returncode 0`, idempotent — the merge already landed) →
  `DONE`. **Fully auto-recovered.**
- **Crash DURING the git merge itself** (process killed mid-write, stale
  `MERGE_HEAD`) → re-run: `git merge feature` returns non-zero ("not concluded /
  MERGE_HEAD exists") → `_merge_into_base` aborts (cleaning the stale state) and
  raises `GitError` → `NEEDS_REVIEW`. Rare; conservative and safe (a human
  confirms), never a silent `DONE`. Documented, not auto-retried.
- **Crash between the `FAILED` and `NEEDS_REVIEW` writes** of the failure path →
  workstream rests in `FAILED` → recovery's FAILED-reconciliation applies the
  retry rule → `READY` → one extra re-run → re-hits the conflict → `NEEDS_REVIEW`.
  Converges (wasteful by one cycle, never hangs).

## Testing

- **`_merge_into_base` (real temp git repo — mirror existing `tests/test_git.py`
  fixtures):**
  - Feature branch with a non-conflicting commit → merge succeeds, no raise, base
    contains the change.
  - Feature branch with a conflicting change to the same lines → raises
    `MergeConflictError`; after the call the base repo is clean (no `MERGE_HEAD`,
    `git status` not mid-merge) — proves the abort ran.
  - A non-conflict git failure (e.g. nonexistent branch) → raises `GitError` (not
    `MergeConflictError`).
  - **Base-branch guard:** the main repo checked out on a NON-base branch → raises
    `GitError` and performs NO merge (assert the base branch is unchanged / the
    feature commit did not land anywhere) — proves the invariant is enforced, not
    assumed.
- **`_handle_success` (orchestrator fixtures; real in-memory DB for status
  assertions, monkeypatch `self._merge_into_base`):**
  - Merge succeeds → status `DONE`, `stats.completed == 1`, `cleanup_workspace`
    called.
  - Merge raises `MergeConflictError` → status `NEEDS_REVIEW` (NOT `DONE`),
    `stats.failed == 1`, `cleanup_workspace` NOT called, `error_message` set on the
    row.
  - Idempotent re-merge: `_merge_into_base` returns normally (simulating "Already
    up to date") on a workstream already at `PR_CREATED` → `DONE` (guards the
    recovery re-run path).
  - auto_pr=False path (starts at `MERGING`) with a succeeding merge → still
    reaches `DONE` (guards the MERGING→PR_CREATED pass-through move).
- Regression: existing `_handle_success` / orchestrator tests stay green; a
  successful workstream still ends `DONE` with the branch merged and the workspace
  cleaned.

## Documentation

- CLAUDE.md orchestrator flow: note the base merge now precedes `DONE` and a merge
  conflict routes to `NEEDS_REVIEW`.
- TODO.md: tick C4 follow-up #2 (base-merge before DONE).

## Out of scope

- The `DECOMPOSING` generation-pid liveness follow-up (C4 follow-up #a) — separate.
- Automated conflict resolution / rebase-before-merge — a conflict goes to a human.
- Reusing `GitManager.merge_branch` / injecting a `GitManager` — rejected above;
  hardening in place preserves obs and minimizes surface.
- Adding `checkout base` to `_merge_into_base` — the read-only base-branch *verify*
  (above) closes the invariant risk without the active-checkout failure surface.
