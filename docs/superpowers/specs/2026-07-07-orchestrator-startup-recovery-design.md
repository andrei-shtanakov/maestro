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

| Stranded state | Recovery path | Rationale |
|---|---|---|
| `DECOMPOSING` | → `READY` | Direct valid transition. The workstream never spawned `run --all`; the spec regenerates on the next tick. Matches C4's own cancel handler (DECOMPOSING → READY, no retry). |
| `RUNNING` | → `FAILED` → `READY` | The `run --all` process died with the orchestrator. `RUNNING → READY` is not a valid transition, so reset via `FAILED` (mirrors `recovery.py`). Re-spawns on the next tick. |
| `MERGING` | → `FAILED` → `NEEDS_REVIEW` | A crash mid-`git merge` can leave a half-merged worktree; a blind re-run over dirty git is unsafe. Surface to a human. |
| `PR_CREATED` | → `FAILED` → `NEEDS_REVIEW` | The PR already exists (`gh pr create` succeeded); re-running would duplicate it. A human confirms/marks it done. |

All target states unstick the loop: `READY` is picked up by `_resolve_ready`;
`NEEDS_REVIEW` is skipped by `_all_workstreams_complete` (so it no longer
blocks completion). Every transition pair is valid in the `WorkstreamStatus`
state machine (`DECOMPOSING→{READY,FAILED}`, `RUNNING→{MERGING,FAILED}`,
`MERGING→{PR_CREATED,FAILED}`, `PR_CREATED→{DONE,FAILED}`,
`FAILED→{READY,NEEDS_REVIEW}`).

### Implementation shape

- `async def _recover_stranded_workstreams(self) -> int` — returns the count
  recovered (for the log line and smoke assertions).
- Query per state via the existing `Database.get_workstreams_by_status`.
- For each stranded workstream: log at INFO which state → which target and
  why, then apply the transition(s).
- Untouched: `PENDING`, `READY`, `DONE`, `ABANDONED`, `NEEDS_REVIEW` — the
  step is idempotent and safe to run on every startup (a clean resume
  recovers zero).
- The `error_message` written on the `FAILED` hop names the recovery cause,
  e.g. "Recovered from stranded RUNNING state after orchestrator restart".
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
those two writes (or between the two writes of this recovery step itself)
leaves a workstream resting in `FAILED`, which then strands exactly like the
other four. So recovery must also reconcile `FAILED`: → `READY` if
`retry_count < max_retries`, else → `NEEDS_REVIEW` (the `_handle_failure`
rule, applied idempotently at startup). This makes recovery TOTAL over every
non-terminal, non-actionable state and makes its own two-write transitions
crash-safe (a partial recovery is finished by the next startup).

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
- Recovery does not try to salvage in-progress `MERGING`/`PR_CREATED` work
  (detect a completed merge, adopt an existing PR) — it conservatively hands
  those to a human. Salvage automation is a possible follow-up.

## Testing

- Unit (real in-memory DB or the orchestrator test fixtures): seed one
  workstream in each stranded state, run `_recover_stranded_workstreams`,
  assert the resulting status: DECOMPOSING→READY, RUNNING→READY,
  MERGING→NEEDS_REVIEW, PR_CREATED→NEEDS_REVIEW, FAILED(retries left)→READY,
  FAILED(exhausted)→NEEDS_REVIEW.
- Idempotence / no-touch: PENDING, READY, DONE, ABANDONED, NEEDS_REVIEW are
  left unchanged; a clean resume recovers 0.
- Return count matches the number actually transitioned.
- No retry consumed: a RUNNING→READY recovery leaves `retry_count` unchanged
  (distinguishes recovery from `_handle_failure`).
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
