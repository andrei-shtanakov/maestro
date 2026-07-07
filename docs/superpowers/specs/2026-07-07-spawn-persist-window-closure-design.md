# Spawn→persist window closure — design

**Date:** 2026-07-07
**Status:** approved
**Context:** Closes the residual risk documented in PR #48 (RUNNING) and #50
(DECOMPOSING) startup-recovery work, and folds in the parked-row pid cleanup
(#50 final-review Minor #1). Recorded as the "uniform spawn→persist window
closure" follow-up in TODO.

## Problem

Startup recovery decides a stranded workstream's fate from its recorded pid
(`process_pid` for RUNNING, `generation_pid` for DECOMPOSING): a live pid →
`NEEDS_REVIEW` (a live orphan may be running — never re-run over it); `None`/dead
→ `READY` (reset and re-run). But the pid is persisted AFTER the subprocess is
spawned, so there is a window:

- **RUNNING** (`_spawn_workstream`): status `RUNNING` is written
  (orchestrator.py:548) BEFORE the `run --all` spawn (:565); `process_pid` is
  written AFTER (:594). A hard crash in that window leaves `RUNNING` +
  `process_pid=NULL` + a live orphan → recovery reads `None` → `READY` → re-run
  races the orphan.
- **DECOMPOSING**: the entry write sets `generation_pid=None` (:487); the
  `plan --full` subprocess spawns later and its pid is persisted via the
  `on_pid` callback. A crash after the spawn but before `on_pid` leaves
  `DECOMPOSING` + `generation_pid=NULL` + a live orphan → recovery → `READY` →
  race.

Both are the same window. This closes them symmetrically with a "spawning"
sentinel pid written BEFORE the spawn, which recovery treats as "a spawn was in
progress — assume a live orphan → NEEDS_REVIEW".

## Change

### 1. In-band `_SPAWNING_SENTINEL`

A module constant `_SPAWNING_SENTINEL = -1` (orchestrator.py, near
`_is_pid_alive`). Stored in the existing `process_pid` / `generation_pid`
`INTEGER` columns (no schema change; both fields are `int | None` with no
`ge=0` constraint). `-1` is never a real pid and is obviously a marker; the
recovery interpretation guards it so it NEVER reaches `os.kill`.

### 2. Write the sentinel BEFORE the spawn, the real pid AFTER

- **RUNNING** — the `READY → RUNNING` transition write (orchestrator.py:546-550)
  gains `process_pid=_SPAWNING_SENTINEL`. The post-spawn write (:594-598)
  overwrites it with the real `process.pid`. The window is now covered: a crash
  between the sentinel write and the real-pid write leaves `RUNNING` +
  `process_pid=SENTINEL`.
- **DECOMPOSING** — the entry write (orchestrator.py:483-489) changes
  `generation_pid=None` to `generation_pid=_SPAWNING_SENTINEL`. This covers the
  DECOMPOSING setup + `plan --full` spawn until `on_pid` overwrites it with the
  real pid, AND still closes the re-decompose stale window (the sentinel
  overwrites any prior real pid on a re-entry — recovery never sees a stale
  prior pid). Chosen over a tighter `on_spawning` callback for zero added
  decomposer plumbing; the wider false-positive window is acceptable (see below).

### 3. Recovery interpretation — `_maybe_live_orphan`

A helper so the sentinel never reaches `os.kill`:

```python
def _maybe_live_orphan(pid: int | None) -> bool:
    """True if the recorded pid indicates a possibly-live orphan: the spawning
    sentinel (a spawn was in progress at the crash) OR a still-alive real pid.
    Checks the sentinel FIRST so it is never passed to os.kill."""
    if pid == _SPAWNING_SENTINEL:
        return True
    return pid is not None and _is_pid_alive(pid)
```

- In-flight loop: `live_orphan = _maybe_live_orphan(orphan_pid)` (replaces the
  inline `orphan_pid is not None and _is_pid_alive(orphan_pid)`).
- **The warning log must distinguish the two cases** so the operator does not
  read a sentinel parking as a confirmed orphan: for a real live pid, keep
  "stranded in %s with a live process (pid %s) — verify and clean it up"; for
  the sentinel (`orphan_pid == _SPAWNING_SENTINEL`), log instead that a spawn was
  in progress at the crash and the state is UNCERTAIN — a subprocess may or may
  not be running; verify before resuming. (Do NOT print `pid -1`.)
- FAILED-reconciliation: the liveness check (currently
  `w.process_pid is not None and _is_pid_alive(w.process_pid)`) becomes
  `_maybe_live_orphan(w.process_pid)` — it reads `process_pid` too, so a
  FAILED row carrying the sentinel (a live-orphan reset interrupted mid two-write)
  must also be recognised.

### 4. Harden `_is_pid_alive` against non-positive pids

Add a guard at the top of `_is_pid_alive`:

```python
    if pid <= 0:
        return False  # never signal a non-positive pid: os.kill(0/-1, …) would
                      # hit the caller's process group / every process.
```

Defence in depth: even if a sentinel (or 0) ever reaches `_is_pid_alive`
directly (bypassing `_maybe_live_orphan`), it can never call
`os.kill(-1/0, …)`. `_maybe_live_orphan` still returns True for the sentinel
(assume-orphan); `_is_pid_alive` answers only "is this REAL pid alive?" (False
for non-positive).

### 5. Parked-row pid cleanup (#50 Minor #1, folded in)

Every recovery write that parks a workstream to `NEEDS_REVIEW` additionally
clears the pids (`process_pid=None, generation_pid=None`) — BOTH the in-flight
live/sentinel-orphan branch AND the FAILED-reconciliation `→ NEEDS_REVIEW` write
(which can also carry the sentinel). This keeps the sentinel (`-1`) / stale pid
off the parked row (the warning log already names the pid for the human). The
intermediate `FAILED` write keeps the pid (crash-safety: a crash before the
`NEEDS_REVIEW` write leaves the row in FAILED with the pid, and
FAILED-reconciliation — now sentinel-aware — re-parks it). The `→ READY` resets
do NOT clear (a re-spawn overwrites `process_pid` / `generation_pid` with a
fresh sentinel anyway).

## Window analysis (post-change)

- Crash in spawn→persist (sentinel written, real pid not yet) → row carries
  SENTINEL → recovery → `NEEDS_REVIEW` (assume orphan). **Closed.**
- New, tinier false-positive window: sentinel written but the spawn has not yet
  happened (crash before `create_subprocess_exec`, or during DECOMPOSING
  workspace setup) → SENTINEL with no orphan → `NEEDS_REVIEW`. This is the SAFE
  direction (a human verifies, finds no orphan, resets to READY) versus the old
  UNSAFE `READY`-over-a-live-orphan. Rare and acceptable.
- MERGING / PR_CREATED are unaffected: they are reached only after the process
  has exited and the real pid was written, so they never carry the sentinel;
  their (dead) `process_pid` → `_maybe_live_orphan` False → `READY`.

## Testing

- **`_is_pid_alive` non-positive guard:** `_is_pid_alive(-1)` and
  `_is_pid_alive(0)` return `False` WITHOUT calling `os.kill` (monkeypatch
  `os.kill` to raise if invoked; assert not called). A positive live/dead pid is
  unchanged.
- **`_maybe_live_orphan`:** `_SPAWNING_SENTINEL` → `True` (and `os.kill` NOT
  called — monkeypatch to raise); `None` → `False`; a live real pid (monkeypatch
  `_is_pid_alive` True) → `True`; a dead real pid → `False`.
- **Recovery — sentinel rows:** seed RUNNING with `process_pid=SENTINEL` →
  `NEEDS_REVIEW`, `stats.failed == 1`, and the parked row's `process_pid` /
  `generation_pid` are `None`. Same for DECOMPOSING with
  `generation_pid=SENTINEL`. FAILED with `process_pid=SENTINEL` → `NEEDS_REVIEW`.
- **Parked-row cleanup:** the existing live-orphan RUNNING test (live real pid →
  NEEDS_REVIEW) now also asserts `process_pid is None` on the parked row.
- **Spawn writes the sentinel:** assert `_spawn_workstream`'s `READY → RUNNING`
  transition update carries `process_pid=_SPAWNING_SENTINEL`, and the DECOMPOSING
  entry update carries `generation_pid=_SPAWNING_SENTINEL` (spy on
  `update_workstream_status` calls, or a focused integration: crash the spawn
  right after the sentinel write and assert recovery parks it).
- Regression: existing recovery tests (dead/None → READY, live real pid →
  NEEDS_REVIEW, FAILED reconciliation by retry rule) stay green.

## NEEDS_REVIEW semantics (operator-facing)

After this change, a recovery-produced `NEEDS_REVIEW` carries TWO possible
meanings, and docs/logs must not imply only the first:

1. **Confirmed live orphan** — a recorded real pid is still alive; a subprocess
   from the previous run is definitely still running. Kill it before resuming.
2. **Spawn-in-progress, state uncertain** (the sentinel) — the crash landed in
   the spawn→persist window; a subprocess MAY or may not have been started, and
   its pid was never recorded. Verify whether anything is running before
   resuming; if not, the workstream can simply be reset to READY.

Both are parked to `NEEDS_REVIEW` because both are unsafe to auto-`READY`-and-
re-run. The warning log distinguishes them (see §3).

## Documentation

- CLAUDE.md orchestrator note: the spawn→persist window is now closed via a
  spawning sentinel (RUNNING + DECOMPOSING); note that recovery `NEEDS_REVIEW`
  can mean "live orphan" OR "spawn-in-progress, uncertain".
- TODO.md: tick the "uniform spawn→persist window closure" follow-up (and its
  folded-in parked-row cleanup).
- Update the residual-risk sections of the #48/#50 specs? No — those are
  historical design docs; the closure is recorded here and in TODO.

## Out of scope

- A tighter `on_spawning` callback for DECOMPOSING (rejected — sentinel-at-entry
  is simpler and the wider false-positive window is safe).
- A dedicated `spawning` column / status value (in-band sentinel is lighter, no
  migration).
- Clearing stale pids on `→ READY` resets (self-overwritten on the next spawn).
