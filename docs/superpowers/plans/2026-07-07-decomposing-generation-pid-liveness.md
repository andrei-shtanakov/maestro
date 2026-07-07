# DECOMPOSING generation-pid liveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the `spec-runner plan --full` pid during DECOMPOSING and liveness-check it in startup recovery, so a re-run never races a surviving orphan generation — symmetric to the RUNNING liveness shipped in PR #48.

**Architecture:** Add a `generation_pid` column/field (mirror of `process_pid`); thread an `on_pid` callback from the orchestrator through `generate_spec`/`_run_spec_runner` to persist it (terminating the process if the persist fails); clear it on DECOMPOSING entry and on every exit; generalize recovery's in-flight liveness check to pick `generation_pid` for DECOMPOSING.

**Tech Stack:** Python 3.12+, uv, asyncio, aiosqlite, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-07-decomposing-generation-pid-liveness-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`; run pytest in the FOREGROUND.
- `generation_pid` = the `plan --full` pid (DECOMPOSING); `process_pid` = the `run --all` pid (RUNNING). Separate fields, never clobber each other.
- `on_pid` is called immediately after `create_subprocess_exec` with NO intervening `await`. If `on_pid` raises, `_terminate` the process and re-raise (never leave an untracked orphan).
- Clear `generation_pid=None` on DECOMPOSING ENTRY (first status write in `_spawn_workstream` — required for the re-decompose window) AND in the `_generate_and_launch` `finally` (cleanliness on success/cancel/failure).
- **The `finally` clear MUST be a same-state write WITHOUT `expected_status`** (`update_workstream_status(id, w.status, generation_pid=None)`) — `update_workstream_status` does not validate transitions, only the optional `expected_status`; adding one would wrongly block the reset after READY/FAILED. Guard with `if w.generation_pid is not None`.
- Recovery: DECOMPOSING with a live `generation_pid` → `FAILED`→`NEEDS_REVIEW` + `stats.failed += 1`; dead/None → `READY` (direct). RUNNING liveness behavior unchanged (only parameterized by state).
- Migration is Mini-R style: append to the `ordered` list, guarded by `PRAGMA table_info`, idempotent.
- Residual risk (spawn→persist window) is NOT closed here — it is deferred to a follow-up ticket (Task 5); do not add a sentinel mechanism.
- Branch: `feat/decomposing-generation-pid-liveness` (exists, spec committed). Full suite green at every commit.

---

### Task 1: `generation_pid` field, column, migration, persistence

**Files:**
- Modify: `maestro/models.py` (add field to `Workstream`), `maestro/database.py` (CREATE TABLE column; migration #5; `allowed` set; row-parse)
- Test: `tests/test_database.py` (round-trip + migration idempotence)

**Interfaces:**
- Produces: `Workstream.generation_pid: int | None`; `update_workstream_status(…, generation_pid=<int|None>)` persists it; a `generation_pid` column on `workstreams`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_database.py` (reuse its existing DB fixture pattern — a
`Database(tmp_path/"x.db")` then `await db.connect()`; mirror how other
workstream tests build a `Workstream`). If a workstream-builder helper exists,
use it; otherwise construct `Workstream(...)` directly with the required fields.

```python
@pytest.mark.anyio
async def test_generation_pid_round_trips(tmp_path) -> None:
    from maestro.database import Database
    from maestro.models import Workstream, WorkstreamStatus

    db = Database(tmp_path / "g.db")
    await db.connect()
    try:
        ws = Workstream(
            id="a", title="a", description="d", scope=["s"],
            branch="feature/a", status=WorkstreamStatus.DECOMPOSING,
            generation_pid=4242,
        )
        await db.create_workstream(ws)
        assert (await db.get_workstream("a")).generation_pid == 4242
        await db.update_workstream_status(
            "a", WorkstreamStatus.DECOMPOSING, generation_pid=None
        )
        assert (await db.get_workstream("a")).generation_pid is None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_generation_pid_migration_idempotent(tmp_path) -> None:
    from maestro.database import Database

    dbfile = tmp_path / "m.db"
    db = Database(dbfile)
    await db.connect()  # applies migrations incl. generation_pid
    await db.close()
    db2 = Database(dbfile)
    await db2.connect()  # second connect must be a no-op, not raise
    try:
        cur = await db2._connection.execute("PRAGMA table_info(workstreams)")
        cols = {r["name"] for r in await cur.fetchall()}
        assert "generation_pid" in cols
    finally:
        await db2.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_database.py -k generation_pid -q`
Expected: FAIL — `Workstream` has no `generation_pid` (and/or column missing).

- [ ] **Step 3: Add the model field**

In `maestro/models.py`, immediately after the `process_pid` field on `Workstream`:

```python
    generation_pid: int | None = Field(
        default=None,
        description="PID of spec-runner plan --full (DECOMPOSING)",
    )
```

- [ ] **Step 4: Add the column, migration, allowed field, and row-parse**

In `maestro/database.py`:

1. In the `CREATE TABLE ... workstreams` column block, after `process_pid INTEGER,`:
```sql
    generation_pid INTEGER,
```

2. Append to the `ordered` migration list (after the version-4 entry):
```python
            (
                5,
                "decomposing_generation_pid",
                self._migrate_workstreams_generation_pid,
            ),
```

3. Add the migration method (next to `_migrate_task_costs_reported_cost`):
```python
    async def _migrate_workstreams_generation_pid(self) -> None:
        """DECOMPOSING liveness: add `generation_pid` to `workstreams`.

        NULL for all pre-existing rows. Idempotent via PRAGMA table_info
        (same shape as the cost-from-log migration).
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute(
            "PRAGMA table_info(workstreams)"
        )
        columns = {row["name"] for row in await cursor.fetchall()}
        if "generation_pid" not in columns:
            await self._connection.execute(
                "ALTER TABLE workstreams ADD COLUMN generation_pid INTEGER"
            )
```

4. In `update_workstream_status`, add `"generation_pid"` to the `allowed` set:
```python
        allowed = {
            "error_message",
            "workspace_path",
            "process_pid",
            "generation_pid",
            "subtask_progress",
            "pr_url",
            "retry_count",
            "branch",
        }
```

5. In the row → `Workstream` parse, after `process_pid=row["process_pid"],`:
```python
        generation_pid=row["generation_pid"],
```

- [ ] **Step 5: Run the tests + gates**

Run: `uv run pytest tests/test_database.py -k generation_pid -q`
Then: `uv run pytest tests/test_database.py -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; existing DB tests green; pyrefly + ruff clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/models.py maestro/database.py tests/test_database.py
git commit -m "feat(db): add workstream generation_pid field + migration (DECOMPOSING liveness)"
```

---

### Task 2: `on_pid` callback in the decomposer (terminate on failure)

**Files:**
- Modify: `maestro/decomposer.py` (`generate_spec`, `_run_spec_runner`; add typing import)
- Test: `tests/test_decomposer.py` (on_pid invoked; on_pid-failure terminates)

**Interfaces:**
- Consumes: existing `_terminate`, `_run_spec_runner`.
- Produces: `generate_spec(self, workstream, workspace_path, timeout_minutes=30, *, on_pid: Callable[[int], Awaitable[None]] | None = None)`; `_run_spec_runner(self, cmd, cwd, timeout_minutes, *, on_pid=None)` calls `await on_pid(proc.pid)` right after spawn, terminating + re-raising on failure.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_decomposer.py`. Monkeypatch `asyncio.create_subprocess_exec`
to return a fake process, so no real `spec-runner` is needed. Mirror the file's
existing `ProjectDecomposer` construction (check how other tests build it).

```python
class _FakeProc:
    def __init__(self, pid=1234, returncode=0):
        self.pid = pid
        self.returncode = returncode
        self.terminated = False

    async def communicate(self):
        return (b"", b"")

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    async def wait(self):
        return self.returncode


@pytest.mark.anyio
async def test_on_pid_called_with_spawned_pid(monkeypatch, tmp_path) -> None:
    from maestro import decomposer as dec_mod

    proc = _FakeProc(pid=5150)

    async def fake_exec(*a, **k):
        return proc

    monkeypatch.setattr(dec_mod.asyncio, "create_subprocess_exec", fake_exec)
    d = _make_decomposer()  # reuse the file's decomposer builder
    seen = []
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text("t\n")

    async def on_pid(pid):
        seen.append(pid)

    await d._run_spec_runner(["spec-runner"], tmp_path, 1, on_pid=on_pid)
    assert seen == [5150]


@pytest.mark.anyio
async def test_on_pid_failure_terminates_and_raises(monkeypatch, tmp_path) -> None:
    from maestro import decomposer as dec_mod

    proc = _FakeProc(pid=5151)

    async def fake_exec(*a, **k):
        return proc

    monkeypatch.setattr(dec_mod.asyncio, "create_subprocess_exec", fake_exec)
    d = _make_decomposer()

    async def bad_on_pid(pid):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await d._run_spec_runner(["spec-runner"], tmp_path, 1, on_pid=bad_on_pid)
    assert proc.terminated is True  # process killed, not orphaned
```

(If the file has no `_make_decomposer` helper, construct `ProjectDecomposer(...)`
inline the way its existing tests do; the two tests only exercise
`_run_spec_runner`, which needs no DB.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_decomposer.py -k on_pid -q`
Expected: FAIL — `_run_spec_runner` has no `on_pid` parameter.

- [ ] **Step 3: Add the typing import**

At the top of `maestro/decomposer.py`, with the other imports:

```python
from collections.abc import Awaitable, Callable
```

- [ ] **Step 4: Thread `on_pid` through `generate_spec`**

Change the `generate_spec` signature (maestro/decomposer.py:285) to add the
keyword-only param, and pass it down. Signature:

```python
    async def generate_spec(
        self,
        workstream: WorkstreamConfig,
        workspace_path: Path,
        timeout_minutes: int = 30,
        *,
        on_pid: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
```

And the internal call (maestro/decomposer.py:333):

```python
            await self._run_spec_runner(
                cmd, workspace_path, timeout_minutes, on_pid=on_pid
            )
```

- [ ] **Step 5: Add `on_pid` to `_run_spec_runner` with terminate-on-failure**

Change `_run_spec_runner` (maestro/decomposer.py:346). Add the param and, right
after the process is created, the guarded `on_pid` call:

```python
    async def _run_spec_runner(
        self,
        cmd: list[str],
        cwd: Path,
        timeout_minutes: int,
        *,
        on_pid: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        """Run a spec-runner subprocess; terminate it on cancel/timeout."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env={**os.environ, **child_env()},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            msg = "spec-runner command not found — is spec-runner installed?"
            raise DecomposerError(msg) from e

        if on_pid is not None:
            # Persist the pid before awaiting the process. If the persist
            # fails we cannot track this process, so terminate it rather than
            # leave an untracked orphan, and propagate the failure.
            try:
                await on_pid(proc.pid)
            except Exception:
                await self._terminate(proc)
                raise
```

Leave the rest of `_run_spec_runner` (the `communicate()`/timeout/returncode
handling) unchanged below this.

- [ ] **Step 6: Run the tests + gates**

Run: `uv run pytest tests/test_decomposer.py -k on_pid -q`
Then: `uv run pytest tests/test_decomposer.py -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; existing decomposer tests green; clean.

- [ ] **Step 7: Commit**

```bash
git add maestro/decomposer.py tests/test_decomposer.py
git commit -m "feat(decomposer): on_pid callback for plan --full; terminate process if persist fails"
```

---

### Task 3: orchestrator wiring — entry clear, on_pid write, finally clear

**Files:**
- Modify: `maestro/orchestrator.py` (`_spawn_workstream` entry clear + on_pid; `_generate_and_launch` finally clear)
- Test: `tests/test_orchestrator.py` (new `TestGenerationPidLifecycle` class)

**Interfaces:**
- Consumes: `generate_spec(..., on_pid=...)` (Task 2), `Workstream.generation_pid` + `update_workstream_status(..., generation_pid=...)` (Task 1).
- Produces: `_spawn_workstream` clears then writes `generation_pid`; `_generate_and_launch` clears it in `finally` on every exit.

- [ ] **Step 1: Write the failing tests (real in-memory DB; mocked decomposer)**

Add to `tests/test_orchestrator.py`. The mocked decomposer's `generate_spec`
invokes the passed `on_pid` with a fake pid, so we can observe the DB writes.
Seed the workstream at READY (so `_spawn_workstream`'s DECOMPOSING transition,
`expected_status=READY`, succeeds).

```python
class TestGenerationPidLifecycle:
    async def _orch_db(self, tmp_path, mock_workspace_mgr, mock_pr_manager,
                       orch_config):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        decomposer = MagicMock()

        async def gen(cfg, ws, *, on_pid=None):
            if on_pid is not None:
                await on_pid(7777)  # simulate plan --full pid

        decomposer.generate_spec = gen
        orch = Orchestrator(
            db=db, workspace_mgr=mock_workspace_mgr, decomposer=decomposer,
            config=orch_config, pr_manager=mock_pr_manager,
        )
        return orch, db

    def _seed(self, zid):
        return Workstream(
            id=zid, title=zid, description="d", scope=["s"],
            branch=f"feature/{zid}", status=WorkstreamStatus.READY,
        )

    @pytest.mark.anyio
    async def test_success_clears_generation_pid(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config,
    ) -> None:
        orch, db = await self._orch_db(tmp_path, mock_workspace_mgr,
                                       mock_pr_manager, orch_config)
        try:
            await db.create_workstream(self._seed("a"))
            await orch._generate_and_launch("a")
            # after a full run the plan pid must be cleared
            assert (await db.get_workstream("a")).generation_pid is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failure_clears_generation_pid(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config,
    ) -> None:
        orch, db = await self._orch_db(tmp_path, mock_workspace_mgr,
                                       mock_pr_manager, orch_config)
        try:
            await db.create_workstream(self._seed("b"))

            async def gen_then_fail(cfg, ws, *, on_pid=None):
                if on_pid is not None:
                    await on_pid(8888)
                raise RuntimeError("spec gen failed")

            orch._decomposer.generate_spec = gen_then_fail
            await orch._generate_and_launch("b")  # routed to _handle_failure
            w = await db.get_workstream("b")
            assert w.generation_pid is None  # cleared despite failure
            assert w.status in (
                WorkstreamStatus.READY, WorkstreamStatus.NEEDS_REVIEW,
            )  # _handle_failure outcome
        finally:
            await db.close()
```

(Adjust `_orch_db`/`_seed` to the file's existing fixtures if a workstream
builder already exists. `mock_workspace_mgr` must return a workspace path and
report `workspace_exists()` False so `create_workspace` is used; the existing
fixture already does this. If `_spawn_workstream`'s later steps — spec-runner
config / commit / `run --all` spawn — need more mocking to reach a clean end,
stub the minimal pieces: e.g. `orch._workspace_mgr.setup_spec_runner`,
`orch._commit_spec_in_workspace`, and the `run --all` spawn. Prefer driving the
real `_generate_and_launch`; note what you stubbed.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestGenerationPidLifecycle -q`
Expected: FAIL — `generation_pid` is never written/cleared yet.

- [ ] **Step 3: Entry clear + on_pid in `_spawn_workstream`**

In `_spawn_workstream`, add `generation_pid=None` to the FIRST DECOMPOSING
transition (the one with `expected_status=workstream.status`):

```python
        # Transition to DECOMPOSING for spec generation (clear any stale
        # generation pid up front — closes the re-decompose window).
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.DECOMPOSING,
            expected_status=workstream.status,
            generation_pid=None,
        )
```

Replace the `generate_spec` call with one that passes an `on_pid` writing the
pid to the DB:

```python
        async def _on_gen_pid(pid: int) -> None:
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.DECOMPOSING,
                generation_pid=pid,
            )

        await self._decomposer.generate_spec(
            workstream_config, workspace, on_pid=_on_gen_pid
        )
```

- [ ] **Step 4: `finally` clear in `_generate_and_launch`**

Extend the `finally` block of `_generate_and_launch`:

```python
        finally:
            self._generating.pop(workstream_id, None)
            # Clear the generation pid on every exit (success/cancel/failure);
            # a stale pid only pollutes REST/dashboard, but keep it clean.
            # Same-state write WITHOUT expected_status (update_workstream_status
            # does not validate transitions — an expected_status here would
            # wrongly block the reset after READY/FAILED).
            with contextlib.suppress(Exception):
                w = await self._db.get_workstream(workstream_id)
                if w.generation_pid is not None:
                    await self._db.update_workstream_status(
                        workstream_id, w.status, generation_pid=None
                    )
```

(`contextlib` is already imported in orchestrator.py.)

- [ ] **Step 5: Run the tests + gates**

Run: `uv run pytest tests/test_orchestrator.py::TestGenerationPidLifecycle -q`
Then: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; full suite green; clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): record + clear plan --full generation_pid across DECOMPOSING"
```

---

### Task 4: recovery liveness for DECOMPOSING

**Files:**
- Modify: `maestro/orchestrator.py` (`_recover_stranded_workstreams` in-flight liveness)
- Test: `tests/test_orchestrator.py` (extend `TestStartupRecovery`)

**Interfaces:**
- Consumes: `Workstream.generation_pid`, `_is_pid_alive`.
- Produces: DECOMPOSING with a live `generation_pid` → NEEDS_REVIEW; dead/None → READY.

- [ ] **Step 1: Write the failing tests**

Add to the existing `TestStartupRecovery` class in `tests/test_orchestrator.py`
(reuse its `_orch_with_db` / `_seed` helpers). `_seed` takes `pid=` for
`process_pid`; add cases seeding `generation_pid` via `model_copy` or by
extending `_seed` with a `gen_pid=` kwarg — match the helper's shape.

```python
    @pytest.mark.anyio
    async def test_decomposing_with_live_generation_pid_needs_review(
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
            ws = self._seed("d", WorkstreamStatus.DECOMPOSING).model_copy(
                update={"generation_pid": 4242}
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("d")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert orch._stats.failed == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_decomposing_with_dead_generation_pid_recovers_ready(
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
            ws = self._seed("d", WorkstreamStatus.DECOMPOSING).model_copy(
                update={"generation_pid": 4242}
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            assert (await db.get_workstream("d")).status == WorkstreamStatus.READY
        finally:
            await db.close()
```

(The existing `test_decomposing_and_finalization_states_recover_to_ready` seeds
DECOMPOSING with `process_pid` and monkeypatches `_is_pid_alive` to False → it
still expects READY; with the new code DECOMPOSING checks `generation_pid`
(None there) → still READY, so that test stays green. Confirm it.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -k generation -q`
Expected: FAIL — DECOMPOSING currently recovers to READY unconditionally.

- [ ] **Step 3: Generalize the liveness pid selection**

In `_recover_stranded_workstreams`, replace the `live_orphan` computation (the
`state is WorkstreamStatus.RUNNING and w.process_pid is not None and
_is_pid_alive(w.process_pid)` block) with a state-parameterized pid:

```python
                    orphan_pid = (
                        w.process_pid
                        if state is WorkstreamStatus.RUNNING
                        else w.generation_pid
                        if state is WorkstreamStatus.DECOMPOSING
                        else None
                    )
                    live_orphan = orphan_pid is not None and _is_pid_alive(
                        orphan_pid
                    )
```

And update the live-orphan warning log to name the state and pid generically
(it currently hard-codes "RUNNING" and `w.process_pid`):

```python
                    if live_orphan:
                        self._logger.warning(
                            "Workstream '%s' stranded in %s with a live "
                            "process (pid %s) after restart — sending to "
                            "NEEDS_REVIEW; verify and clean it up before resume",
                            w.id,
                            state.value,
                            orphan_pid,
                        )
```

Leave the `elif state is WorkstreamStatus.DECOMPOSING:` (dead → READY) and the
`else:` (FAILED→READY) branches unchanged.

- [ ] **Step 4: Run the tests + gates**

Run: `uv run pytest tests/test_orchestrator.py::TestStartupRecovery -q`
Then: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS (new + existing recovery tests, incl. the RUNNING liveness ones); clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): liveness-check DECOMPOSING via generation_pid in recovery"
```

---

### Task 5: docs, TODO tick + follow-up ticket, final gates, PR

**Files:**
- Modify: `CLAUDE.md`, `TODO.md`

- [ ] **Step 1: CLAUDE.md**

In the `orchestrator.py` bullet (the startup-recovery sentence added in PR #48),
extend it to note DECOMPOSING is now liveness-checked too:

```markdown
On resume it first reconciles workstreams stranded by a prior hard crash (DECOMPOSING/RUNNING/MERGING/PR_CREATED → READY; a live-orphan RUNNING (process_pid) or DECOMPOSING (generation_pid) → NEEDS_REVIEW; FAILED by the retry rule) so the main loop can advance them.
```

- [ ] **Step 2: TODO.md — tick #a, add the window follow-up**

Tick the C4 follow-up #a entry (`(a) DECOMPOSING orphan liveness …`) `[x]` with
`(closed by feat/decomposing-generation-pid-liveness)`. Add a new follow-up:

```markdown
- [ ] Uniform spawn→persist window closure (RUNNING + DECOMPOSING): a hard crash
      between spawning the subprocess and persisting its pid leaves status set
      with pid=NULL and a live orphan → recovery reads None → READY → re-run
      races the orphan. Close both windows symmetrically (e.g. a "spawning"
      sentinel pid recovery treats as "assume live → NEEDS_REVIEW"), including
      the already-merged RUNNING path. (From the gen-pid liveness spec's
      residual-risk section.)
```

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
git commit -m "docs: DECOMPOSING generation-pid liveness shipped — C4 follow-up #a ticked"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/decomposing-generation-pid-liveness
gh pr create --title "feat(orchestrator): DECOMPOSING generation-pid liveness (C4 follow-up #a)" --body "$(cat <<'EOF'
## Summary
- Startup recovery reset a `DECOMPOSING` workstream to `READY` unconditionally, unlike `RUNNING` (which liveness-checks `process_pid`). But the `spec-runner plan --full` generation subprocess can outlive a hard crash, and its pid was never recorded — so a re-run could spawn a second `plan --full` over the same workspace while the orphan was still writing `spec/`. This records the generation pid and liveness-checks `DECOMPOSING`, symmetric to PR #48's `RUNNING` check
- New `generation_pid` column/field (mirror of `process_pid`, Mini-R migration #5); `generation_pid` = `plan --full` pid, `process_pid` = `run --all` pid — separate, never clobbered
- Pid is persisted via an `on_pid` callback threaded through `generate_spec`/`_run_spec_runner`; if the persist fails the process is `_terminate`d and the error re-raised (never an untracked orphan)
- `generation_pid` is cleared on DECOMPOSING entry (closes the re-decompose stale window) and in `_generate_and_launch`'s `finally` (uniform cleanup on success/cancel/failure)
- Recovery: `DECOMPOSING` with a live `generation_pid` → `NEEDS_REVIEW` (`stats.failed++`); dead/None → `READY`. The `RUNNING` path is unchanged (only parameterized by state)

## Residual risk (documented, deferred)
The spawn→persist window (crash between spawning and persisting the pid → `NULL` + live orphan) is NOT closed here — it is identical to the already-shipped `RUNNING` window in PR #48. A follow-up ticket (in TODO) closes both symmetrically via a "spawning" sentinel. DECOMPOSING is the lowest-severity orphan class (`spec/` is regenerated; the tasks.md post-condition + spec-runner parse catch gross corruption).

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] `generation_pid` round-trips; migration adds the column and is idempotent
- [ ] `on_pid` invoked with the spawned pid; `on_pid` failure terminates the process and raises
- [ ] `generation_pid` cleared on DECOMPOSING entry and on every `_generate_and_launch` exit (success/cancel/failure)
- [ ] recovery: DECOMPOSING live gen-pid → NEEDS_REVIEW + failed++; dead/None → READY; RUNNING liveness unchanged

Completes the C4 orchestrator crash-safety follow-ups (#1 PR #48, #2 PR #49, #a here).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: field+column+migration+allowed+parse → Task 1; on_pid callback + terminate-on-failure → Task 2; entry clear + on_pid write + finally clear → Task 3; recovery liveness generalization → Task 4; residual-risk follow-up ticket + docs → Task 5.
- Type consistency: `generation_pid: int | None`; `on_pid: Callable[[int], Awaitable[None]] | None`; `_run_spec_runner(..., *, on_pid=None)`; `generate_spec(..., *, on_pid=None)`; recovery `orphan_pid` selection — consistent across tasks.
- The `finally` clear is explicitly a same-state write WITHOUT `expected_status` (global constraint + Task 3 Step 4 comment) — the reviewer's operational caveat.
- Real-DB tests (Tasks 1/3/4) assert persisted state; decomposer tests (Task 2) use a fake proc, no real spec-runner.
- Five tasks, each an independent reviewer gate: DB layer, decomposer, orchestrator generation path, recovery, docs.
