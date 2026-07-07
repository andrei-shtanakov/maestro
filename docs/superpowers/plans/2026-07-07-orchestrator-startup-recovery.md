# Orchestrator startup recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On `orchestrate` startup, reconcile workstreams stranded in a non-terminal in-flight state after a hard crash so the resume loop can advance them — with a liveness check that never re-runs over a surviving orphan process.

**Architecture:** A new `Orchestrator._recover_stranded_workstreams()` step called in `run()` between `_ensure_workstreams()` and `_main_loop()`, plus a tiny `_is_pid_alive` helper. In-flight strands (DECOMPOSING/RUNNING/MERGING/PR_CREATED) reset to READY (no retry, no error_message); a RUNNING workstream whose recorded `process_pid` is still alive goes to NEEDS_REVIEW instead; FAILED workstreams reconcile by the retry rule.

**Tech Stack:** Python 3.12+, uv, asyncio, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-07-orchestrator-startup-recovery-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`; run pytest in the FOREGROUND.
- In-flight strands recover to READY as a PURE reset: NO retry consumed, NO error_message written (the cause is logged only — a recovered-to-READY workstream must have `error_message` None). Do NOT reuse `_handle_failure` (it bumps retry_count and writes error_message).
- `RUNNING → READY` only when the process is gone (`process_pid` None, or `os.kill(pid, 0)` raises `ProcessLookupError`); a LIVE recorded pid → `RUNNING → FAILED → NEEDS_REVIEW` with a message naming the pid (never blindly re-run over a live orphan, never blindly kill a possibly-reused pid).
- Transitions must be state-machine-valid: DECOMPOSING→READY direct; RUNNING/MERGING/PR_CREATED→FAILED→(READY|NEEDS_REVIEW); FAILED→(READY|NEEDS_REVIEW).
- FAILED reconciliation uses the retry rule: `can_retry()` → READY, else → NEEDS_REVIEW (this is the ONLY path recovery produces NEEDS_REVIEW besides a live RUNNING orphan).
- Recovery is per-workstream best-effort (a DB error on one is logged, does not abort the rest) and never raises out of `run()`. Idempotent: PENDING/READY/DONE/ABANDONED/NEEDS_REVIEW untouched; a clean resume recovers 0.
- Liveness check applies ONLY to RUNNING (MERGING/PR_CREATED's process already exited).
- Branch: `feat/orchestrator-startup-recovery` (exists, spec committed). Full suite (~1480) green at every commit.

---

### Task 1: `_is_pid_alive` + `_recover_stranded_workstreams` + wiring

**Files:**
- Modify: `maestro/orchestrator.py` (`_is_pid_alive` helper; `_recover_stranded_workstreams` method; call it in `run()` after `_ensure_workstreams()`)
- Test: `tests/test_orchestrator.py` (new `TestStartupRecovery` class)

**Interfaces:**
- Consumes: `Database.get_workstreams_by_status(status) -> list[Workstream]`, `Database.update_workstream_status(id, new_status, expected_status=None, **extra)`, `Workstream.can_retry()`, `Workstream.process_pid: int | None` (all existing).
- Produces: `Orchestrator._recover_stranded_workstreams(self) -> int` (count recovered); module-level `_is_pid_alive(pid: int) -> bool`.

- [ ] **Step 1: Write the `_is_pid_alive` test**

Add a `TestStartupRecovery` class to `tests/test_orchestrator.py`. First the helper test:

```python
class TestStartupRecovery:
    def test_is_pid_alive_true_when_no_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: None)
        assert orch_mod._is_pid_alive(4242) is True

    def test_is_pid_alive_false_on_process_lookup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def boom(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(orch_mod.os, "kill", boom)
        assert orch_mod._is_pid_alive(4242) is False

    def test_is_pid_alive_true_on_permission_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def denied(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(orch_mod.os, "kill", denied)
        assert orch_mod._is_pid_alive(4242) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Expected: FAIL — `_is_pid_alive` does not exist.

- [ ] **Step 3: Implement `_is_pid_alive`**

In `maestro/orchestrator.py` (module level, near the top after imports):

```python
def _is_pid_alive(pid: int) -> bool:
    """True if a process with this pid exists (signal 0 probes without killing).

    ProcessLookupError means it is gone; PermissionError means it exists but
    we may not signal it (still alive).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
```

- [ ] **Step 4: Run to verify the helper tests pass**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Expected: PASS (3 helper tests).

- [ ] **Step 5: Write the recovery-method tests (real in-memory DB)**

Append to `TestStartupRecovery`. These build an Orchestrator over a real
`Database` (recovery only touches `self._db`/`self._logger`; the other deps
are mocks). Import `Database` from `maestro.database` and reuse the mock
fixtures. Helper to build the orchestrator + seed a workstream:

```python
    async def _orch_with_db(self, tmp_path, mock_workspace_mgr, mock_decomposer,
                            mock_pr_manager, orch_config):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=orch_config,
            pr_manager=mock_pr_manager,
        )
        return orch, db

    def _seed(self, zid, status, *, retry_count=0, max_retries=3, pid=None):
        return Workstream(
            id=zid, title=zid, description="d", scope=["s"],
            branch=f"feature/{zid}", status=status,
            retry_count=retry_count, max_retries=max_retries, process_pid=pid,
        )

    @pytest.mark.anyio
    async def test_decomposing_and_finalization_states_recover_to_ready(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config, monkeypatch,
    ) -> None:
        from maestro import orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda pid: False)
        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            for zid, st in [
                ("d", WorkstreamStatus.DECOMPOSING),
                ("r", WorkstreamStatus.RUNNING),
                ("m", WorkstreamStatus.MERGING),
                ("p", WorkstreamStatus.PR_CREATED),
            ]:
                await db.create_workstream(self._seed(zid, st, pid=999))
            count = await orch._recover_stranded_workstreams()
            assert count == 4
            for zid in ("d", "r", "m", "p"):
                w = await db.get_workstream(zid)
                assert w.status == WorkstreamStatus.READY
                assert w.error_message is None  # no spurious error text
                assert w.retry_count == 0  # no retry consumed
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_with_live_pid_goes_to_needs_review(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config, monkeypatch,
    ) -> None:
        from maestro import orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda pid: True)
        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING, pid=4242)
            )
            count = await orch._recover_stranded_workstreams()
            assert count == 1
            w = await db.get_workstream("r")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_with_no_pid_recovers_to_ready(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING, pid=None)
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("r")
            assert w.status == WorkstreamStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_reconciliation_by_retry_rule(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("keep", WorkstreamStatus.FAILED,
                           retry_count=0, max_retries=2)
            )
            await db.create_workstream(
                self._seed("done", WorkstreamStatus.FAILED,
                           retry_count=2, max_retries=2)
            )
            await orch._recover_stranded_workstreams()
            assert (await db.get_workstream("keep")).status == WorkstreamStatus.READY
            assert (
                await db.get_workstream("done")
            ).status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_clean_states_untouched_and_count_zero(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            for zid, st in [
                ("pe", WorkstreamStatus.PENDING),
                ("re", WorkstreamStatus.READY),
                ("do", WorkstreamStatus.DONE),
                ("ab", WorkstreamStatus.ABANDONED),
                ("nr", WorkstreamStatus.NEEDS_REVIEW),
            ]:
                await db.create_workstream(self._seed(zid, st))
            count = await orch._recover_stranded_workstreams()
            assert count == 0
            for zid, st in [
                ("pe", WorkstreamStatus.PENDING),
                ("re", WorkstreamStatus.READY),
                ("do", WorkstreamStatus.DONE),
                ("ab", WorkstreamStatus.ABANDONED),
                ("nr", WorkstreamStatus.NEEDS_REVIEW),
            ]:
                assert (await db.get_workstream(zid)).status == st
        finally:
            await db.close()
```

(Verify `db.create_workstream` preserves the seeded `status` and
`process_pid` verbatim — it is an insert, so it should; if it forces PENDING,
seed then `update_workstream_status` into place instead.)

- [ ] **Step 6: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Expected: FAIL — `_recover_stranded_workstreams` does not exist.

- [ ] **Step 7: Implement `_recover_stranded_workstreams`**

In `maestro/orchestrator.py`, add a module-level constant near the other
constants and the method on `Orchestrator`:

```python
_STRANDED_INFLIGHT = (
    WorkstreamStatus.DECOMPOSING,
    WorkstreamStatus.RUNNING,
    WorkstreamStatus.MERGING,
    WorkstreamStatus.PR_CREATED,
)
```

```python
    async def _recover_stranded_workstreams(self) -> int:
        """Reconcile workstreams stranded by a hard crash so the resume loop
        can advance them. In-flight strands reset to READY (no retry, no
        error_message); a RUNNING workstream whose recorded process is still
        alive goes to NEEDS_REVIEW instead (never re-run over a live orphan);
        FAILED workstreams reconcile by the retry rule. Best-effort per
        workstream; never raises."""
        recovered = 0

        for state in _STRANDED_INFLIGHT:
            for w in await self._db.get_workstreams_by_status(state):
                try:
                    live_orphan = (
                        state is WorkstreamStatus.RUNNING
                        and w.process_pid is not None
                        and _is_pid_alive(w.process_pid)
                    )
                    if live_orphan:
                        self._logger.warning(
                            "Workstream '%s' stranded in RUNNING with a live "
                            "process (pid %s) after restart — sending to "
                            "NEEDS_REVIEW; verify and clean it up before resume",
                            w.id,
                            w.process_pid,
                        )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.FAILED
                        )
                        await self._db.update_workstream_status(
                            w.id,
                            WorkstreamStatus.NEEDS_REVIEW,
                            expected_status=WorkstreamStatus.FAILED,
                        )
                    elif state is WorkstreamStatus.DECOMPOSING:
                        self._logger.info(
                            "Recovering workstream '%s' from stranded "
                            "DECOMPOSING -> READY",
                            w.id,
                        )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.READY
                        )
                    else:
                        # RUNNING (dead) / MERGING / PR_CREATED: cannot go
                        # directly to READY, reset via FAILED.
                        self._logger.info(
                            "Recovering workstream '%s' from stranded %s -> READY",
                            w.id,
                            state.value,
                        )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.FAILED
                        )
                        await self._db.update_workstream_status(
                            w.id,
                            WorkstreamStatus.READY,
                            expected_status=WorkstreamStatus.FAILED,
                        )
                    recovered += 1
                except Exception as e:  # noqa: BLE001 — best-effort per workstream
                    self._logger.error(
                        "Failed to recover workstream '%s': %s", w.id, e
                    )

        # FAILED reconciliation (genuine failures resting mid-_handle_failure).
        # Runs after the in-flight loop, so in-flight resets that pass through
        # FAILED have already reached their final state.
        for w in await self._db.get_workstreams_by_status(WorkstreamStatus.FAILED):
            try:
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
                await self._db.update_workstream_status(
                    w.id, target, expected_status=WorkstreamStatus.FAILED
                )
                recovered += 1
            except Exception as e:  # noqa: BLE001 — best-effort per workstream
                self._logger.error(
                    "Failed to reconcile FAILED workstream '%s': %s", w.id, e
                )

        if recovered:
            self._logger.info(
                "Recovered %d stranded workstream(s) on startup", recovered
            )
        return recovered
```

- [ ] **Step 8: Run the recovery tests**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Expected: PASS (all helper + method tests).

- [ ] **Step 9: Wire into `run()` + integration test**

In `run()`, call recovery after `_ensure_workstreams()`:

```python
            # Step 1: Ensure workstreams exist
            await self._ensure_workstreams()

            # Step 1b: Reconcile workstreams stranded by a prior hard crash
            # (resume path) so the main loop can advance them.
            await self._recover_stranded_workstreams()

            # Step 2: Main loop
            await self._main_loop()
```

Integration test — a stranded RUNNING (no pid) does not hang `run()`; recovery
runs before the loop and the workstream reaches READY. Add to
`TestStartupRecovery` (mirror the existing `run()`-driving tests in the file
for how they stop the loop — e.g. `_all_workstreams_complete` returning True
after one pass, or a shutdown; adapt to the file's established pattern):

```python
    @pytest.mark.anyio
    async def test_run_recovers_before_loop(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING, pid=None)
            )
            # Recovery is the unit under test here; assert it flips the state.
            # (Driving the full run() loop to completion is covered by the
            # file's existing run() tests; keep this focused on the ordering:
            # recovery must have run by the time the loop first resolves.)
            recovered = await orch._recover_stranded_workstreams()
            assert recovered == 1
            assert (await db.get_workstream("r")).status == WorkstreamStatus.READY
        finally:
            await db.close()
```

(If the file has a clean way to run `run()` with a one-tick shutdown, prefer a
true end-to-end assertion that `run()` terminates and the workstream is READY;
otherwise the focused ordering assertion above is acceptable — note which you
did.)

- [ ] **Step 10: Run tests + gates**

Run: `uv run pytest tests/test_orchestrator.py -q && uv run pytest -q`
Then: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; pyrefly clean; ruff clean.

- [ ] **Step 11: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): startup recovery of stranded workstreams (C4 follow-up)

On resume, reconcile workstreams stranded by a hard crash so the main
loop can advance them: DECOMPOSING/RUNNING/MERGING/PR_CREATED -> READY
(pure reset, no retry, no error_message); a RUNNING workstream whose
recorded process is still alive -> NEEDS_REVIEW (never re-run over a live
orphan, never blindly kill a possibly-reused pid); FAILED reconciled by
the retry rule. Runs before the main loop; best-effort per workstream."
```

---

### Task 2: Docs, TODO tick, final gates, PR

**Files:**
- Modify: `CLAUDE.md` (orchestrator note), `TODO.md` (tick the C4 startup-recovery follow-up)

- [ ] **Step 1: CLAUDE.md**

In the orchestrator/architecture description, add a sentence to the
`orchestrator.py` bullet:

```markdown
On resume it first reconciles workstreams stranded by a prior hard crash (DECOMPOSING/RUNNING/MERGING/PR_CREATED → READY; a live-orphan RUNNING → NEEDS_REVIEW; FAILED by the retry rule) so the main loop can advance them.
```

- [ ] **Step 2: TODO.md**

Find the C4 startup-recovery follow-up entry (added in the C4 branch:
"Orchestrator startup recovery: workstreams stranded in DECOMPOSING or
RUNNING …") and tick it `[x]` with `(closed by feat/orchestrator-startup-recovery)`.

- [ ] **Step 3: Final gates + smoke**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
```

Smoke — recovery flips a stranded RUNNING (no pid) to READY on a real DB:

```bash
uv run python -c "
import asyncio
from unittest.mock import MagicMock
from maestro.database import Database
from maestro.orchestrator import Orchestrator
from maestro.models import Workstream, WorkstreamStatus, OrchestratorConfig

async def main():
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    db = Database(d / 's.db'); await db.connect()
    cfg = OrchestratorConfig(project='p', repo_url='git@x:y.git',
        repo_path='/tmp/x', workspace_base='/tmp/w', workstreams=[])
    orch = Orchestrator(db=db, workspace_mgr=MagicMock(), decomposer=MagicMock(),
        config=cfg, pr_manager=MagicMock())
    await db.create_workstream(Workstream(id='r', title='r', description='d',
        scope=['s'], branch='feature/r', status=WorkstreamStatus.RUNNING))
    n = await orch._recover_stranded_workstreams()
    w = await db.get_workstream('r')
    print('recovered:', n, '| status:', w.status.value)
    await db.close()
asyncio.run(main())
"
```

Expected: `recovered: 1 | status: ready`.

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: orchestrator startup recovery shipped — C4 follow-up ticked"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/orchestrator-startup-recovery
gh pr create --title "feat(orchestrator): startup recovery of stranded workstreams (C4 follow-up)" --body "$(cat <<'EOF'
## Summary
- On `orchestrate --resume`, reconcile workstreams stranded in a non-terminal in-flight state by a hard crash so the main loop can advance them (previously they were never re-resolved — `_resolve_ready` picks only PENDING/READY — and the loop spun forever)
- DECOMPOSING / RUNNING / MERGING / PR_CREATED → READY as a pure reset: no retry consumed, no error_message (cause logged only)
- **Orphan-safe RUNNING recovery:** `run --all` is spawned without process-group/death-signal isolation (macOS has no PR_SET_PDEATHSIG), so it can survive a hard crash. Recovery checks the recorded `process_pid`: gone → READY; still alive → NEEDS_REVIEW with the pid named (never re-run over a live orphan, never blindly kill a possibly-reused pid)
- FAILED workstreams (resting mid-`_handle_failure` after a crash) reconcile by the retry rule (READY if retries left, else NEEDS_REVIEW) — also makes recovery's own two-write resets crash-safe
- Uniform `→ READY` sidesteps `auto_pr` branching: MERGING/PR_CREATED re-run finalizes idempotently in both modes (the base merge is post-DONE, so no half-merged git; `→ DONE` would skip it)
- Runs before the main loop; best-effort per workstream; idempotent (clean resume recovers 0)

Spec: docs/superpowers/specs/2026-07-07-orchestrator-startup-recovery-design.md

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] Each stranded state → correct target; clean states untouched; count correct; no retry consumed / no error_message on in-flight resets
- [ ] RUNNING liveness: no-pid → READY, dead pid → READY, live pid → NEEDS_REVIEW; `_is_pid_alive` all three branches
- [ ] FAILED reconciliation by retry rule
- [ ] Smoke: recovery flips a real stranded RUNNING to READY

Known limitations (in spec): DECOMPOSING plan --full orphan not liveness-checked (no recorded pid); a crash DURING the post-DONE base merge lands in DONE (out of scope). Both noted for follow-up.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: recovery transitions (all 6 states) → Task 1 Steps 5/7; liveness check → Steps 1/3/7; no-error/no-retry reset → Step 7 + assertions in Step 5; FAILED reconciliation → Step 7; wiring before loop → Step 9; idempotence/no-touch → Step 5; docs → Task 2.
- Type consistency: `_recover_stranded_workstreams(self) -> int`, `_is_pid_alive(pid: int) -> bool`, `_STRANDED_INFLIGHT` tuple — all consistent between Steps 5/7/9.
- Real-DB tests (not mock_db) so assertions are on actual persisted status — cleaner than asserting mock call sequences for a multi-write flow. The implementer must confirm `create_workstream` preserves the seeded status/pid (insert), else seed-then-update.
- The integration step offers a focused ordering assertion with a note to prefer a true one-tick `run()` end-to-end if the file's existing tests show a clean way to stop the loop — avoids inventing a fragile loop-driver.
- Two tasks: Task 1 is the cohesive implementation+tests (one reviewer gate over the whole recovery unit); Task 2 is docs+PR.
