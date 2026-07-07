# DECOMPOSING generation-pid liveness — design

**Date:** 2026-07-07
**Status:** approved
**Context:** C4 follow-up #a (from the orchestrator startup-recovery final review,
PR #48, "known limitations"). Recorded in TODO.

## Problem

Startup recovery (`_recover_stranded_workstreams`, PR #48) resets a workstream
stranded in `DECOMPOSING` to `READY` **unconditionally** — unlike the `RUNNING`
branch, which liveness-checks the recorded `process_pid` and sends a live orphan to
`NEEDS_REVIEW` instead of re-running over it.

During `DECOMPOSING`, `Orchestrator._spawn_workstream` runs
`decomposer.generate_spec`, which spawns a `spec-runner plan --full` subprocess
(`_run_spec_runner`, `create_subprocess_exec`) that writes `spec/{requirements,
design,tasks}.md` into the workspace. That subprocess is NOT started in its own
session and has no death signal, so a hard crash of the orchestrator can leave it
running as an orphan — exactly like the `run --all` case. But its PID is never
recorded (`Workstream.process_pid` holds only the `run --all` pid). So on resume,
recovery resets `DECOMPOSING → READY`, the re-run spawns a SECOND `plan --full`
over the same workspace while the orphan is still writing `spec/` → a race that can
corrupt the generated spec.

This closes the gap symmetrically with the `RUNNING` liveness check: record the
generation pid and liveness-check `DECOMPOSING` in recovery.

## Change

### 1. Schema + model: `generation_pid`

Add a nullable `generation_pid` to the workstream, mirroring `process_pid`:

- `Workstream.generation_pid: int | None = Field(default=None, …)` (models.py, beside
  `process_pid`).
- `CREATE TABLE workstreams`: add `generation_pid INTEGER` (for fresh DBs).
- Migration (Mini-R linear journal): append
  `(5, "decomposing_generation_pid", self._migrate_workstreams_generation_pid)` to
  the `ordered` list; the body is a guarded
  `PRAGMA table_info(workstreams)` → `ALTER TABLE workstreams ADD COLUMN
  generation_pid INTEGER` (idempotent — same shape as migration #4
  `_migrate_task_costs_reported_cost`).
- `update_workstream_status`'s `allowed` extra-fields set: add `"generation_pid"`.
- Row → `Workstream` parsing: add `generation_pid=row["generation_pid"]`.

`generation_pid` = the `plan --full` pid (DECOMPOSING); `process_pid` = the
`run --all` pid (RUNNING). Separate fields — the two subprocess lifecycles never
clobber each other's pid.

### 2. Record the pid: `on_pid` callback

The pid is born inside `decomposer._run_spec_runner` (`proc.pid`), but only the
orchestrator has DB access. Thread a callback through (keeps the decomposer
DB-agnostic):

- `generate_spec(self, workstream, workspace_path, timeout_minutes=30, *,
  on_pid: Callable[[int], Awaitable[None]] | None = None)` — pass `on_pid` down to
  `_run_spec_runner`.
- `_run_spec_runner(self, cmd, cwd, timeout_minutes, *, on_pid=None)`: immediately
  after `create_subprocess_exec` succeeds, `if on_pid is not None: await
  on_pid(proc.pid)` — with NO intervening `await`, so the window between spawn and
  persist is minimal (see Residual risk).
- `_spawn_workstream` passes
  `on_pid=lambda pid: self._db.update_workstream_status(workstream_id,
  WorkstreamStatus.DECOMPOSING, generation_pid=pid)`.

### 3. Clear `generation_pid` on entry AND exit of DECOMPOSING

- **On entry:** the first `update_workstream_status(…, DECOMPOSING, …)` in
  `_spawn_workstream` also sets `generation_pid=None`. This closes the re-decompose
  stale window: a workstream can re-enter `DECOMPOSING` (recovery `RUNNING→READY` →
  `_spawn_workstream` → `DECOMPOSING` again); clearing before the new `plan --full`
  spawns guarantees `generation_pid` is `None` (recovery reads "no process" → READY,
  safe) or the current plan pid — never a stale prior pid.
- **On exit:** the `DECOMPOSING → READY` transition (after a successful
  `generate_spec`, in `_spawn_workstream`) also sets `generation_pid=None`. The
  `plan --full` process has already exited by then; clearing keeps the row clean so
  REST/dashboard don't display a stale pid.

### 4. Recovery: liveness-check DECOMPOSING (generalize the in-flight branch)

In `_recover_stranded_workstreams`, the in-flight loop currently hard-codes the
liveness check to `state is RUNNING` / `process_pid`. Generalize the pid selection:

```python
pid = (
    w.process_pid if state is WorkstreamStatus.RUNNING
    else w.generation_pid if state is WorkstreamStatus.DECOMPOSING
    else None
)
live_orphan = pid is not None and _is_pid_alive(pid)
```

- `live_orphan` (either state) → `→ FAILED → NEEDS_REVIEW`, `stats.failed += 1`; log
  naming the state and pid ("… stranded in %s with a live process (pid %s) …").
- `DECOMPOSING` with a dead/None pid → `→ READY` (direct transition, unchanged).
- `RUNNING` (dead) / `MERGING` / `PR_CREATED` → `→ FAILED → READY` (unchanged).

`DECOMPOSING → {READY, FAILED}` and `FAILED → NEEDS_REVIEW` are all valid
transitions. The `RUNNING` liveness behavior is unchanged (same code path, now
parameterized by state).

## Residual risk (accepted, documented)

**The spawn→persist window is NOT closed by this change.** Between
`create_subprocess_exec` returning (the `plan --full` OS process exists) and the
`await on_pid(proc.pid)` DB write completing, a hard crash leaves `DECOMPOSING` with
`generation_pid = NULL` while a live orphan exists — recovery then reads `None` →
`READY` → re-run can race the orphan.

This is accepted because:

- **It is identical to the already-shipped `RUNNING` window.** In PR #48, `RUNNING`
  is written (orchestrator.py:~378) before the spawn and `process_pid` after
  (~427); a crash in that window likewise yields `RUNNING` + `process_pid=NULL` →
  `READY`. Closing only the DECOMPOSING window here would introduce an asymmetry the
  design explicitly avoids.
- **The window is minimal** — `create_subprocess_exec` is immediately followed by
  `await on_pid(...)` with no intervening `await`.
- **DECOMPOSING is the lowest-severity orphan class** — the orphan writes `spec/`,
  which the re-run regenerates; the post-condition (`spec/tasks.md` must exist) plus
  spec-runner's own parse catch gross corruption.

**Follow-up ticket (recorded in TODO):** *uniform spawn→persist window closure for
RUNNING + DECOMPOSING* — e.g. write a "spawning" sentinel pid before the spawn that
recovery interprets as "assume live orphan → NEEDS_REVIEW", applied symmetrically to
both states (and to the already-merged RUNNING path). Deferred so both windows are
closed together rather than introducing an asymmetry now.

## Testing

- **`_run_spec_runner` / `generate_spec` (`on_pid`)** — spawn a fast stand-in for
  `spec-runner` (e.g. a tiny script on PATH, or monkeypatch
  `create_subprocess_exec`) and assert `on_pid` is invoked once with the spawned
  process's real pid. Assert generation proceeds unchanged when `on_pid=None`
  (back-compat).
- **`_spawn_workstream` pid lifecycle** — entry clears `generation_pid` to None
  before generation; `on_pid` writes the plan pid during DECOMPOSING; the
  `DECOMPOSING → READY` exit clears it back to None. (Use a mocked decomposer whose
  `generate_spec` invokes the passed `on_pid` with a fake pid, and a real in-memory
  DB; assert the persisted `generation_pid` at each phase.)
- **Recovery** — DECOMPOSING with a live `generation_pid` (monkeypatch
  `_is_pid_alive` True) → `NEEDS_REVIEW`, `stats.failed == 1`; DECOMPOSING with a
  dead pid (`_is_pid_alive` False) or `generation_pid=None` → `READY`; the RUNNING
  liveness path is unchanged (regression: RUNNING live → NEEDS_REVIEW still holds).
- **Migration** — a DB created before this change (no `generation_pid` column) gains
  it on connect; a second connect is a no-op (idempotent); the migration is
  journaled at version 5.

## Documentation

- CLAUDE.md: note that DECOMPOSING recovery is now liveness-checked (generation pid),
  symmetric to RUNNING.
- TODO.md: tick C4 follow-up #a; add the new "uniform spawn→persist window closure"
  follow-up.

## Out of scope

- Closing the spawn→persist window (deferred — see Residual risk; must be done
  symmetrically for RUNNING + DECOMPOSING).
- Process-group / death-signal isolation of `plan --full` (a broader change; the
  liveness check is the agreed mechanism, per #48).
- Changing the `RUNNING` liveness behavior (unchanged; only parameterized by state).
