# Orchestrator startup recovery — design

**Date:** 2026-07-07
**Status:** approved
**Context:** C4 final-review Minor #4 (PR #46). Recorded follow-up in TODO.

## Problem

On `maestro orchestrate --resume`, the orchestrator loads existing workstreams
from the DB as-is (`_ensure_workstreams` returns early when workstreams
exist) and never reconciles their state. But the main loop only advances
workstreams that are actionable:

- `_resolve_ready` picks only `PENDING` / `READY`.
- `_all_workstreams_complete` treats only `DONE` / `ABANDONED` as terminal
  (and skips `NEEDS_REVIEW`).

So a workstream left in a NON-terminal, in-flight state after a hard crash
(SIGKILL, power loss, OOM) — `DECOMPOSING`, `RUNNING`, `MERGING`, or
`PR_CREATED` — is never re-resolved and never counted complete. The resume
loop spins forever making no progress on it (or hangs waiting for it). The
scheduler mode already solves the analogous problem with `StateRecovery`
(`recovery.py`, wired at cli.py:469); the orchestrator has no equivalent.

## Change

A new `_recover_stranded_workstreams()` step in `Orchestrator.run()`, called
between `_ensure_workstreams()` and `_main_loop()`. It scans for workstreams
in the four stranded states and transitions each to a resumable or
human-review state via direct `update_workstream_status` writes — a pure
state reset that consumes NO retry (a hard crash is not a workstream failure;
this matches `recovery.py`'s `_transition_to_ready`, which does not bump
`retry_count`).

### Recovery transitions

All four in-flight strands recover to `READY` (re-run) as a pure state reset
that consumes NO retry — a hard crash is not a workstream failure (matches
`recovery.py`, which resets without bumping `retry_count`). `FAILED` is a
separate case (a genuine failure resting mid-`_handle_failure`) and uses the
retry rule.

| Stranded state | Recovery path | Rationale |
|---|---|---|
| `DECOMPOSING` | → `READY` | Direct valid transition. Never spawned `run --all`; the spec regenerates on the next tick. Matches C4's cancel handler. |
| `RUNNING` | → `FAILED` → `READY` | The `run --all` process died with the orchestrator. `RUNNING → READY` is not a valid transition, so reset via `FAILED` (mirrors `recovery.py`). Re-spawns and re-runs. |
| `MERGING` | → `FAILED` → `READY` | `run --all` succeeded but finalization was interrupted. Re-running re-enters `_handle_success` cleanly. **No half-merged-git risk**: `_merge_into_base` is the LAST step of `_handle_success`, AFTER the `DONE` transition — a `MERGING` strand has not started the base merge. Recovering to `DONE` instead would SKIP the base merge (work stranded on the feature branch); re-run does it idempotently. |
| `PR_CREATED` | → `FAILED` → `READY` | Same finalization window. Re-run's `_handle_success`: PR creation is idempotent-tolerant (an existing PR raises `PRManagerError`, already handled as "PR may exist" — no duplicate), and `_merge_into_base` is a no-op if already merged. Recovering to `DONE` would skip the base merge. |

`READY` is picked up by `_resolve_ready`; the re-run's `_handle_success` does
the PR + base-merge + `DONE` idempotently. Every transition pair is valid in
the `WorkstreamStatus` state machine (`DECOMPOSING→{READY,FAILED}`,
`RUNNING/MERGING→{...,FAILED}`, `PR_CREATED→{...,FAILED}`,
`FAILED→{READY,NEEDS_REVIEW}`).

**Why not branch on `auto_pr`.** Under `auto_pr=False`, `PR_CREATED` is a
pass-through with no real PR (`pr_url` is None) — so blanket `NEEDS_REVIEW`
would wrongly send a normal non-PR resume to human review. But recovering
those to `DONE` skips the base merge. Uniform `→ READY` re-run finalizes
correctly in BOTH `auto_pr` modes with no branching and no base-merge gap.
The only cost: re-running an `auto_pr=True` `PR_CREATED` workstream re-runs
`run --all` (fast — tasks already done) and loses the recorded `pr_url` on
the re-created row (the PR itself is not duplicated). Acceptable.

### Implementation shape

- `async def _recover_stranded_workstreams(self) -> int` — returns the count
  recovered (for the log line and smoke assertions).
- Query per state via the existing `Database.get_workstreams_by_status`.
- For each stranded workstream: log at INFO which state → which target and
  why, then apply the transition(s).
- Untouched: `PENDING`, `READY`, `DONE`, `ABANDONED`, `NEEDS_REVIEW` — the
  step is idempotent and safe to run on every startup (a clean resume
  recovers zero).
- **No `error_message` on recovery transitions.** The recovery cause is
  logged only; the status writes carry `error_message=None`. Writing a cause
  on the `FAILED` hop would persist onto the resulting `READY` row, making a
  cleanly recovered workstream look like it errored in the UI. (This is why
  recovery does not reuse `_handle_failure`, which writes an error_message —
  see below.)
- **Does NOT reuse `_handle_failure`** for the in-flight strands: that path
  bumps `retry_count` and writes an error_message. Recovery is a no-retry,
  no-error reset. It uses direct `update_workstream_status` writes (mirroring
  `recovery.py`'s `_transition_to_ready`).
- Does NOT reuse scheduler `StateRecovery` (that operates on `Task` with
  different DB methods and a different state machine). This is orchestrator-
  side, following the same shape.

## A fifth state: FAILED reconciliation (added during design)

The scope approved four states (DECOMPOSING, RUNNING, MERGING, PR_CREATED).
Designing the two-write transitions surfaced a fifth that MUST be handled or
recovery creates a new strand class:

`FAILED` is non-terminal but is NOT resumed by `_resolve_ready` (only
PENDING/READY). In normal operation `FAILED` is transient — `_handle_failure`
writes `FAILED` then immediately `READY`/`NEEDS_REVIEW`. But a crash between
those two writes (or between the two writes of this recovery step's own
in-flight resets) leaves a workstream resting in `FAILED`, which then strands
exactly like the other four. So recovery must also reconcile `FAILED`.

Unlike the in-flight strands (a crash, reset to READY, no retry consumed), a
workstream resting in `FAILED` IS a genuine failure — `_handle_failure` put
it there. So it uses the retry rule: → `READY` if `retry_count < max_retries`,
else → `NEEDS_REVIEW`. This is the ONLY path where recovery produces
`NEEDS_REVIEW`, and only for genuinely retry-exhausted work. It makes recovery
TOTAL over every non-terminal, non-actionable state and makes the in-flight
two-write resets crash-safe (a partial reset is finished by the next startup).

## Error handling

- Each workstream's recovery is independent; a DB error on one is logged and
  does not abort the others (best-effort, mirroring `_cleanup`'s per-item
  suppression). Recovery never raises out of `run()`.
- Two-write transitions apply `FAILED` first (valid from RUNNING/MERGING/
  PR_CREATED), then the target. If the second write is interrupted, the
  workstream rests in `FAILED` and the FAILED-reconciliation branch of the
  next startup completes it — no permanent strand.

## Known limitations (out of scope)

- **Dirty worktree on `RUNNING → READY` re-run.** The re-spawn reuses the
  existing worktree (`_spawn_workstream` checks `workspace_exists`), and the
  prior `run --all` may have left partial commits / spec-runner state. This
  is a pre-existing property of the READY re-spawn path (the C4 cancel
  handler has the same behavior), NOT introduced here. Worktree cleanup /
  reset on recovery is a separate ticket if it proves to matter in practice.
- **A crash DURING `_merge_into_base` lands the workstream in `DONE`, not a
  recoverable state.** `_merge_into_base` runs after the `DONE` transition, so
  if the base merge is interrupted the workstream already shows `DONE` while
  the feature branch is not merged into base. Recovery skips terminal states,
  so this is not covered. Pre-existing (the base-merge-after-DONE ordering
  predates this change) and out of scope; noted for a possible separate
  ticket (move the base merge before the `DONE` transition, or add a
  merged-into-base check).
- Re-running a `PR_CREATED` (auto_pr=True) strand loses the recorded `pr_url`
  on the re-created row; the PR itself is not duplicated (the existing
  "PR may exist" handling absorbs it). Acceptable — the URL is recoverable
  from the branch/GitHub.

## Testing

- Unit (real in-memory DB or the orchestrator test fixtures): seed one
  workstream in each stranded state, run `_recover_stranded_workstreams`,
  assert the resulting status: DECOMPOSING→READY, RUNNING→READY,
  MERGING→READY, PR_CREATED→READY, FAILED(retries left)→READY,
  FAILED(exhausted)→NEEDS_REVIEW.
- No spurious error_message: a recovered-to-READY workstream has
  `error_message` None (the cause is logged, not persisted).
- No retry consumed for in-flight strands: a RUNNING→READY (and MERGING,
  PR_CREATED, DECOMPOSING) recovery leaves `retry_count` unchanged
  (distinguishes the reset from `_handle_failure`). The FAILED path DOES
  follow the retry rule (READY vs NEEDS_REVIEW by retry_count).
- Idempotence / no-touch: PENDING, READY, DONE, ABANDONED, NEEDS_REVIEW are
  left unchanged; a clean resume recovers 0.
- Return count matches the number actually transitioned.
- Integration: `run()` on a DB containing a stranded RUNNING workstream (with
  a mocked decomposer/spawner and a shutdown after one tick) does NOT hang —
  recovery runs before the loop, the workstream reaches READY and is picked
  up. Assert the recovery step ran before `_main_loop`.

## Documentation

- CLAUDE.md orchestrator/architecture note: mention startup recovery of
  stranded workstreams on resume.
- TODO.md: tick the C4 startup-recovery follow-up.

## Out of scope

- Scheduler mode (already has `StateRecovery`).
- Worktree/git cleanup on re-run (pre-existing behavior; separate ticket).
- Automated salvage of MERGING/PR_CREATED (conservative NEEDS_REVIEW instead).
