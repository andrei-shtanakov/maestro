# Spawn→persist window closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the spawn→persist window (a hard crash between spawning a subprocess and persisting its pid leaves status set + pid=NULL + a live orphan → recovery resets to READY → races the orphan) via a "spawning" sentinel pid, symmetrically for RUNNING and DECOMPOSING.

**Architecture:** A module constant `_SPAWNING_SENTINEL = -1` is written into `process_pid`/`generation_pid` BEFORE each spawn and overwritten with the real pid after. Recovery interprets the sentinel via `_maybe_live_orphan` (never passes it to `os.kill`) as a possible live orphan → NEEDS_REVIEW; `_is_pid_alive` is hardened to reject non-positive pids; parked-to-NEEDS_REVIEW rows clear their pids.

**Tech Stack:** Python 3.12+, uv, asyncio, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-07-spawn-persist-window-closure-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`; run pytest in the FOREGROUND.
- `_SPAWNING_SENTINEL = -1`. It is NEVER passed to `os.kill`: `_maybe_live_orphan` checks `== _SPAWNING_SENTINEL` FIRST and returns True; `_is_pid_alive` returns False for `pid <= 0` (defence in depth).
- Sentinel written BEFORE the spawn, real pid AFTER: RUNNING on the READY→RUNNING transition write; DECOMPOSING on the entry write (replaces `generation_pid=None` — also closes the re-decompose stale window).
- Recovery: in-flight loop uses `_maybe_live_orphan(orphan_pid)`; FAILED-reconciliation uses `_maybe_live_orphan(w.process_pid)`. Every recovery write that parks to NEEDS_REVIEW also clears `process_pid=None, generation_pid=None`; `→ READY` resets do NOT clear.
- Differentiated warning log: sentinel case → "spawn in progress at the crash, state uncertain" (do NOT print `pid -1`); real live pid → "live process (pid N)".
- NEEDS_REVIEW from recovery now means EITHER a confirmed live orphan OR spawn-in-progress-uncertain (doc note).
- MERGING/PR_CREATED never carry the sentinel (their process already exited) — unaffected.
- Branch: `feat/spawn-persist-window-closure` (exists, spec committed). Full suite green at every commit.

---

### Task 1: `_SPAWNING_SENTINEL` + `_is_pid_alive` guard + `_maybe_live_orphan`

**Files:**
- Modify: `maestro/orchestrator.py` (constant near `_is_pid_alive`; guard in `_is_pid_alive`; new `_maybe_live_orphan`)
- Test: `tests/test_orchestrator.py` (extend `TestStartupRecovery`)

**Interfaces:**
- Produces: `_SPAWNING_SENTINEL: int = -1`; `_is_pid_alive(pid)` returns False for `pid <= 0`; `_maybe_live_orphan(pid: int | None) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py` inside `TestStartupRecovery` (import the
module as `orch_mod` — the existing `_is_pid_alive` tests already do):

```python
    def test_is_pid_alive_rejects_nonpositive_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def boom(pid, sig):
            raise AssertionError(f"os.kill must not be called (pid={pid})")

        monkeypatch.setattr(orch_mod.os, "kill", boom)
        assert orch_mod._is_pid_alive(-1) is False
        assert orch_mod._is_pid_alive(0) is False

    def test_maybe_live_orphan_sentinel_is_true_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def boom(pid, sig):
            raise AssertionError("os.kill must not be called for the sentinel")

        monkeypatch.setattr(orch_mod.os, "kill", boom)
        assert orch_mod._maybe_live_orphan(orch_mod._SPAWNING_SENTINEL) is True

    def test_maybe_live_orphan_none_and_real_pids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        assert orch_mod._maybe_live_orphan(None) is False
        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda pid: True)
        assert orch_mod._maybe_live_orphan(4242) is True
        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda pid: False)
        assert orch_mod._maybe_live_orphan(4242) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -k "nonpositive or maybe_live" -q`
Expected: FAIL — `_maybe_live_orphan` / `_SPAWNING_SENTINEL` don't exist; `_is_pid_alive(-1)` currently calls `os.kill(-1, 0)` → the `boom` fires.

- [ ] **Step 3: Add the constant + guard `_is_pid_alive`**

In `maestro/orchestrator.py`, add the constant just above `_is_pid_alive`:

```python
_SPAWNING_SENTINEL = -1
"""Placeholder pid written into ``process_pid`` / ``generation_pid`` BEFORE a
subprocess spawn and overwritten with the real pid after. A recovery that finds
it treats the workstream as a possible live orphan (a spawn was in progress at
the crash). Never passed to ``os.kill`` — see ``_maybe_live_orphan`` and the
``pid <= 0`` guard in ``_is_pid_alive``."""
```

And add the guard at the top of `_is_pid_alive`'s body (before the `try`):

```python
    if pid <= 0:
        # Never signal a non-positive pid: os.kill(0/-1, …) would hit the
        # caller's process group / every process. A real pid is always > 0.
        return False
```

- [ ] **Step 4: Add `_maybe_live_orphan`**

Add below `_is_pid_alive`:

```python
def _maybe_live_orphan(pid: int | None) -> bool:
    """True if the recorded pid indicates a possibly-live orphan: the spawning
    sentinel (a spawn was in progress at the crash) or a still-alive real pid.

    Checks the sentinel FIRST so it is never passed to os.kill.
    """
    if pid == _SPAWNING_SENTINEL:
        return True
    return pid is not None and _is_pid_alive(pid)
```

- [ ] **Step 5: Run tests + gates**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -k "nonpositive or maybe_live" -q`
Then: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): spawning sentinel + _maybe_live_orphan + non-positive pid guard"
```

---

### Task 2: Recovery interprets the sentinel + differentiated log + parked-row cleanup

**Files:**
- Modify: `maestro/orchestrator.py` (`_recover_stranded_workstreams`: in-flight loop, warning log, NEEDS_REVIEW writes; FAILED-reconciliation)
- Test: `tests/test_orchestrator.py` (extend `TestStartupRecovery`)

**Interfaces:**
- Consumes: `_SPAWNING_SENTINEL`, `_maybe_live_orphan` (Task 1).
- Produces: recovery parks a sentinel/live orphan to NEEDS_REVIEW with pids cleared; FAILED-reconciliation is sentinel-aware.

- [ ] **Step 1: Write the failing tests**

Add to `TestStartupRecovery`. The class's `_seed(zid, status, *, pid=None)` sets
`process_pid=pid`; seed `generation_pid` via `model_copy`:

```python
    @pytest.mark.anyio
    async def test_running_sentinel_pid_parks_needs_review_and_clears(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING,
                           pid=orch_mod._SPAWNING_SENTINEL)
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("r")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert orch._stats.failed == 1
            assert w.process_pid is None      # parked-row cleanup
            assert w.generation_pid is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_decomposing_sentinel_gen_pid_parks_needs_review(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            ws = self._seed("d", WorkstreamStatus.DECOMPOSING).model_copy(
                update={"generation_pid": orch_mod._SPAWNING_SENTINEL}
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("d")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert w.generation_pid is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_sentinel_pid_parks_needs_review(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("f", WorkstreamStatus.FAILED,
                           pid=orch_mod._SPAWNING_SENTINEL)
            )
            await orch._recover_stranded_workstreams()
            assert (await db.get_workstream("f")).status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()
```

Also strengthen the EXISTING live-real-pid RUNNING test
(`test_running_with_live_pid_goes_to_needs_review`, which monkeypatches
`_is_pid_alive` to True) — after recovery, add
`assert (await db.get_workstream(<id>)).process_pid is None` (parked-row cleanup
now clears it). Adjust the id/variable to match that test.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Expected: the three sentinel tests FAIL — a SENTINEL pid currently reaches
`_is_pid_alive(-1)` (now False after Task 1) → treated as dead → the RUNNING
sentinel resets to READY (not NEEDS_REVIEW); and pids aren't cleared.

- [ ] **Step 3: Use `_maybe_live_orphan` + differentiated log + clear pids (in-flight loop)**

In `_recover_stranded_workstreams`, the in-flight loop. Replace the
`live_orphan = ...` line and the `if live_orphan:` block:

```python
                    live_orphan = _maybe_live_orphan(orphan_pid)
                    if live_orphan:
                        if orphan_pid == _SPAWNING_SENTINEL:
                            self._logger.warning(
                                "Workstream '%s' stranded in %s with a spawn in "
                                "progress at the crash — state uncertain (a "
                                "subprocess may or may not be running); sending "
                                "to NEEDS_REVIEW, verify before resuming",
                                w.id,
                                state.value,
                            )
                        else:
                            self._logger.warning(
                                "Workstream '%s' stranded in %s with a live "
                                "process (pid %s) after restart — sending to "
                                "NEEDS_REVIEW; verify and clean it up before resume",
                                w.id,
                                state.value,
                                orphan_pid,
                            )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.FAILED
                        )
                        await self._db.update_workstream_status(
                            w.id,
                            WorkstreamStatus.NEEDS_REVIEW,
                            expected_status=WorkstreamStatus.FAILED,
                            process_pid=None,
                            generation_pid=None,
                        )
                        # Parked for review — signal via exit code + summary,
                        # matching _handle_failure's NEEDS_REVIEW accounting.
                        self._stats.failed += 1
```

(Leave the `elif`/`else` READY branches unchanged.)

- [ ] **Step 4: Make FAILED-reconciliation sentinel-aware + clear pids on its NEEDS_REVIEW write**

In the FAILED-reconciliation loop, replace the liveness check and the write.
Change `if w.process_pid is not None and _is_pid_alive(w.process_pid):` to
`if _maybe_live_orphan(w.process_pid):`, and clear pids on the NEEDS_REVIEW
write. The block becomes:

```python
                if _maybe_live_orphan(w.process_pid):
                    # A FAILED row can be an in-flight reset interrupted mid
                    # two-write (X->FAILED committed, target write lost). If its
                    # recorded pid is alive OR the spawning sentinel, it may be a
                    # live orphan — never reset to READY. Park for review.
                    target = WorkstreamStatus.NEEDS_REVIEW
                else:
                    target = (
                        WorkstreamStatus.READY
                        if w.can_retry()
                        else WorkstreamStatus.NEEDS_REVIEW
                    )
                self._logger.info(
                    "Reconciling FAILED workstream '%s' -> %s",
                    w.id,
                    target.value,
                )
                if target is WorkstreamStatus.NEEDS_REVIEW:
                    await self._db.update_workstream_status(
                        w.id,
                        WorkstreamStatus.NEEDS_REVIEW,
                        expected_status=WorkstreamStatus.FAILED,
                        process_pid=None,
                        generation_pid=None,
                    )
                    # Parked for review — signal via exit code + summary.
                    self._stats.failed += 1
                else:
                    await self._db.update_workstream_status(
                        w.id,
                        WorkstreamStatus.READY,
                        expected_status=WorkstreamStatus.FAILED,
                    )
```

(This replaces the current single `update_workstream_status(w.id, target, …)` +
the trailing `if target is NEEDS_REVIEW: self._stats.failed += 1`.)

- [ ] **Step 5: Run tests + gates**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Then: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS (new sentinel tests + strengthened live-pid test + existing
recovery tests, incl. dead/None → READY and FAILED-by-retry-rule); clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): recovery parks the spawning sentinel to NEEDS_REVIEW + clears parked-row pids"
```

---

### Task 3: Spawn writes the sentinel before the real pid

**Files:**
- Modify: `maestro/orchestrator.py` (`_spawn_workstream`: DECOMPOSING entry write; READY→RUNNING transition write)
- Test: `tests/test_orchestrator.py` (extend `TestGenerationPidLifecycle` or a focused class)

**Interfaces:**
- Consumes: `_SPAWNING_SENTINEL` (Task 1).
- Produces: `_spawn_workstream` writes the sentinel into `generation_pid` (DECOMPOSING entry) and `process_pid` (RUNNING transition) before the real pid.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py`. Reuse the `TestGenerationPidLifecycle`
harness (real in-memory DB, a mocked decomposer whose `generate_spec` invokes
`on_pid`). Spy on the DB writes by wrapping `update_workstream_status`, OR assert
the persisted intermediate values are not observable — so instead assert the
SENTINEL is written by intercepting the calls. A call-capturing wrapper:

```python
class TestSpawnWritesSentinel:
    @pytest.mark.anyio
    async def test_decomposing_entry_writes_sentinel_generation_pid(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        calls: list[dict] = []
        real = db.update_workstream_status

        async def spy(zid, status, *, expected_status=None, **extra):
            calls.append({"status": status, **extra})
            return await real(zid, status, expected_status=expected_status, **extra)

        db.update_workstream_status = spy  # type: ignore[method-assign]

        async def gen(cfg, ws, *, on_pid=None):
            if on_pid is not None:
                await on_pid(7777)

        decomposer = MagicMock()
        decomposer.generate_spec = gen
        orch = Orchestrator(
            db=db, workspace_mgr=mock_workspace_mgr, decomposer=decomposer,
            config=orch_config, pr_manager=mock_pr_manager,
        )
        try:
            await db.create_workstream(
                Workstream(id="a", title="a", description="d", scope=["s"],
                           branch="feature/a", status=WorkstreamStatus.READY)
            )
            # Drive only up to the RUNNING spawn; monkeypatch the subprocess
            # spawn so no real spec-runner runs. If _spawn_workstream reaches
            # the run --all spawn, stub asyncio.create_subprocess_exec + the
            # workspace/commit helpers as TestGenerationPidLifecycle does.
            await orch._generate_and_launch("a")
        finally:
            await db.close()

        # DECOMPOSING entry carried the sentinel; RUNNING transition too.
        dec = [c for c in calls if c["status"] == WorkstreamStatus.DECOMPOSING]
        assert dec and dec[0].get("generation_pid") == orch_mod._SPAWNING_SENTINEL
        run_writes = [c for c in calls if c["status"] == WorkstreamStatus.RUNNING]
        assert any(
            c.get("process_pid") == orch_mod._SPAWNING_SENTINEL for c in run_writes
        )
```

(This mirrors `TestGenerationPidLifecycle`'s mocking. Match the exact stubs that
class uses to let `_generate_and_launch` reach the RUNNING spawn without a real
subprocess — e.g. `mock_workspace_mgr` returning a path + `workspace_exists`
False, stubbing `_commit_spec_in_workspace` and `asyncio.create_subprocess_exec`.
If reaching the RUNNING write proves too fiddly, split into two focused asserts:
the DECOMPOSING entry sentinel (early, easy) here, and the RUNNING sentinel via a
smaller drive; note what you did.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::TestSpawnWritesSentinel -q`
Expected: FAIL — the DECOMPOSING entry currently writes `generation_pid=None`
(not the sentinel) and the RUNNING transition writes no `process_pid`.

- [ ] **Step 3: Write the sentinel on the DECOMPOSING entry**

In `_spawn_workstream`, the first DECOMPOSING transition (maestro/orchestrator.py
~483-489) — change `generation_pid=None` to the sentinel:

```python
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.DECOMPOSING,
            expected_status=workstream.status,
            generation_pid=_SPAWNING_SENTINEL,
        )
```

Update the adjacent comment to note it also marks a spawn-in-progress:
`# Transition to DECOMPOSING; write the spawning sentinel up front — it marks a`
`# spawn-in-progress AND overwrites any stale prior generation pid (re-decompose).`

- [ ] **Step 4: Write the sentinel on the READY→RUNNING transition**

In `_spawn_workstream`, the READY→RUNNING transition (maestro/orchestrator.py
~546-550) — add the sentinel:

```python
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.RUNNING,
            expected_status=WorkstreamStatus.READY,
            process_pid=_SPAWNING_SENTINEL,
        )
```

(The post-spawn write at ~594-598, `process_pid=process.pid`, already overwrites
it with the real pid — unchanged.)

- [ ] **Step 5: Run tests + gates**

Run: `uv run pytest tests/test_orchestrator.py::TestSpawnWritesSentinel -q`
Then: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; full suite green (the generation-pid lifecycle tests still hold —
the entry now writes the sentinel instead of None, but `on_pid` overwrites it and
the finally clears it; if a lifecycle test asserted the entry value was None
mid-flight, update it to the sentinel); clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): write the spawning sentinel before the RUNNING/DECOMPOSING spawn"
```

---

### Task 4: Docs, TODO tick, final gates, PR

**Files:**
- Modify: `CLAUDE.md`, `TODO.md`

- [ ] **Step 1: CLAUDE.md**

In the `orchestrator.py` startup-recovery sentence, append a note about the
window closure and the dual NEEDS_REVIEW meaning:

```markdown
The spawn→persist window (crash between spawning a subprocess and persisting its pid) is closed by a spawning sentinel written before the spawn: recovery reads it as a possible live orphan → NEEDS_REVIEW. A recovery NEEDS_REVIEW thus means either a confirmed live orphan or a spawn-in-progress (state uncertain).
```

- [ ] **Step 2: TODO.md — tick the follow-up**

Find the "Uniform spawn→persist window closure (RUNNING + DECOMPOSING)" entry and
tick it `[x]` with `(closed by feat/spawn-persist-window-closure)` — note the
folded-in parked-row cleanup is included.

- [ ] **Step 3: Final gates**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
```

Expected: full suite green; pyrefly 0; ruff clean.

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: spawn->persist window closure shipped (sentinel)"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/spawn-persist-window-closure
gh pr create --title "feat(orchestrator): close the spawn->persist window with a spawning sentinel" --body "$(cat <<'EOF'
## Summary
- Recovery decided a stranded workstream's fate from its recorded pid, but the pid is persisted AFTER the subprocess spawns — so a hard crash in the spawn→persist window left the row with status set + pid=NULL + a live orphan, and recovery reset it to READY → re-run raced the orphan. This closes both windows (RUNNING and DECOMPOSING) symmetrically
- `_SPAWNING_SENTINEL = -1` is written into `process_pid` (RUNNING, on the READY→RUNNING transition) / `generation_pid` (DECOMPOSING, on the entry write) BEFORE the spawn, and overwritten with the real pid after
- Recovery interprets it via `_maybe_live_orphan` (checks the sentinel FIRST — it is never passed to `os.kill`) as a possible live orphan → NEEDS_REVIEW; `_is_pid_alive` is hardened to return False for `pid <= 0` (defence in depth against ever signalling a non-positive pid). FAILED-reconciliation is sentinel-aware too
- Parked-to-NEEDS_REVIEW rows now clear their pids (folds in the #50 final-review Minor); the intermediate FAILED write keeps the pid (crash-safe convergence)
- The warning log distinguishes "spawn in progress, state uncertain" (sentinel — no `pid -1` printed) from "live process (pid N)". A recovery NEEDS_REVIEW now means EITHER a confirmed live orphan OR a spawn-in-progress
- Trade: the old unsafe window (READY over a live orphan) becomes a safe false-positive window (a crash after the sentinel write but before the spawn → NEEDS_REVIEW with no orphan — a human verifies and resets). MERGING/PR_CREATED never carry the sentinel (their process already exited)

Closes the "uniform spawn→persist window closure" follow-up from #48/#50. Completes the C4 orchestrator crash-safety line.

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] `_is_pid_alive(-1)`/`(0)` → False WITHOUT calling `os.kill`; `_maybe_live_orphan(sentinel)` → True without signalling; None → False; real live/dead → True/False
- [ ] recovery: RUNNING/DECOMPOSING/FAILED carrying the sentinel → NEEDS_REVIEW; parked rows have `process_pid`/`generation_pid` cleared; existing live-real-pid → NEEDS_REVIEW (now also cleared); dead/None → READY unchanged
- [ ] spawn writes the sentinel on the DECOMPOSING entry and the READY→RUNNING transition (before the real pid)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: sentinel const + `_is_pid_alive` guard + `_maybe_live_orphan` → Task 1; recovery interpretation + differentiated log + parked-row cleanup + FAILED-reconciliation → Task 2; spawn writes (RUNNING + DECOMPOSING) → Task 3; NEEDS_REVIEW dual-semantics doc + TODO → Task 4.
- Type consistency: `_SPAWNING_SENTINEL: int`; `_maybe_live_orphan(pid: int | None) -> bool`; `_is_pid_alive(pid) -> bool` (guard added). Consistent across tasks.
- Load-bearing safety: the sentinel is checked before `os.kill` in TWO layers (`_maybe_live_orphan` first-check + `_is_pid_alive` pid<=0 guard) — Task 1 tests both.
- Task 3's spawn test drives `_generate_and_launch` with the `TestGenerationPidLifecycle` mocking idiom; the note allows splitting the RUNNING assert if the full drive is fiddly.
- Four tasks: helpers / recovery / spawn-writes / docs — each an independent reviewer gate.
