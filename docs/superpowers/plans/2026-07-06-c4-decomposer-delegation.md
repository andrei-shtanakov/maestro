# C4: decomposer → spec-runner delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Maestro's built-in `SPEC_GENERATION_PROMPT` with a delegation to `spec-runner plan --full`, and — because `--full` runs ~6 min — run that generation as an async per-workstream background task so it does not serialize Mode-2's parallel pipeline.

**Architecture:** `ProjectDecomposer.generate_spec` becomes an async `spec-runner plan --full` subprocess (cancellation-safe, post-condition-checked). The orchestrator launches it as a background `asyncio.Task` per workstream, counting in-flight generations against `max_concurrent`, routing failures through `_handle_failure` and shutdown-cancellations back to READY.

**Tech Stack:** Python 3.12+, uv, asyncio subprocess, Typer CLI, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-06-c4-decomposer-delegation-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`.
- Delegation is `spec-runner plan --full` (measurement proved no clean 1-call path). Command: `["spec-runner", "plan", "--full", "--from-file", <desc>, "--no-branch", "--no-commit", "--no-interactive"]` + `["--budget", str(b)]` when a budget is set.
- `report_status`/spec-runner ownership: spec-runner owns the `tasks.md` format; `SPEC_GENERATION_PROMPT` and `_write_spec_files` are DELETED.
- Budget default `1.0` (`SpecRunnerConfig.spec_gen_budget_usd: float | None = 1.0`; `None` disables the cap).
- Cancellation MUST kill the spec-runner subprocess (no orphan burning tokens). Generation failures route through `_handle_failure` (retry accounting), NOT a raw terminal FAILED. Shutdown cancel → workstream back to READY, `retry_count` unchanged.
- Post-condition: after a zero exit, `spec/tasks.md` must exist or `DecomposerError`.
- Temp desc file: `NamedTemporaryFile("w", encoding="utf-8", delete=False)` OUTSIDE the workspace; `Path(tmp).unlink(missing_ok=True)` in `finally`.
- Version pin stays a doc constant (runtime gate deferred); golden drift test is a required WEEKLY CI job.
- Branch: `feat/c4-decomposer-delegation` (exists; spec + steward proposal + I1 doc committed). Full suite (~1458) green at every commit.

---

### Task 1: Async `generate_spec` delegation in the decomposer

**Files:**
- Modify: `maestro/decomposer.py` (delete `SPEC_GENERATION_PROMPT` ~line 85 + `_write_spec_files`; rewrite `generate_spec` ~line 327; `__init__` ~line 144 gains a budget param; imports)
- Modify: `maestro/orchestrator.py:334` (call site: `await` the now-async method)
- Test: `tests/test_decomposer.py` (rewrite `TestGenerateSpec`), `tests/test_orchestrator.py:64-69` (mock decomposer → AsyncMock)

**Interfaces:**
- Produces: `async def generate_spec(self, workstream: WorkstreamConfig, workspace_path: Path) -> None`; `ProjectDecomposer.__init__(self, repo_path, claude_command="claude", spec_gen_budget_usd: float | None = 1.0)`. Task 3 calls `await generate_spec(...)`; Task 2 passes `spec_gen_budget_usd`.

- [ ] **Step 1: Rewrite the decomposer generate_spec tests**

In `tests/test_decomposer.py`, replace the entire `TestGenerateSpec` class (the old tests assert Claude-CLI/markers behavior that no longer exists). New class:

```python
class TestGenerateSpec:
    """generate_spec delegates to `spec-runner plan --full` (async)."""

    @pytest.fixture
    def workstream(self) -> WorkstreamConfig:
        return WorkstreamConfig(
            id="ws1",
            title="Feature X",
            description="Do the thing",
            scope=["src/x.py", "tests/test_x.py"],
        )

    def _fake_proc(self, returncode: int = 0, stderr: bytes = b""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=returncode)
        return proc

    @pytest.mark.anyio
    async def test_invokes_spec_runner_plan_full(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        workspace.mkdir()
        (workspace / "spec").mkdir()
        (workspace / "spec" / "tasks.md").write_text("# tasks\n", encoding="utf-8")
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as exec_mock:
            await dec.generate_spec(workstream, workspace)
        cmd = list(exec_mock.call_args[0])
        assert cmd[:4] == ["spec-runner", "plan", "--full", "--from-file"]
        assert "--no-branch" in cmd and "--no-commit" in cmd
        assert "--no-interactive" in cmd
        assert "--budget" in cmd and cmd[cmd.index("--budget") + 1] == "1.0"
        assert exec_mock.call_args.kwargs["cwd"] == workspace

    @pytest.mark.anyio
    async def test_description_file_has_workstream_fields(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / "tasks.md").write_text("x", encoding="utf-8")
        dec = ProjectDecomposer(repo_path=temp_dir)
        captured = {}

        async def fake_exec(*args, **kwargs):
            desc_path = args[args.index("--from-file") + 1]
            captured["text"] = Path(desc_path).read_text(encoding="utf-8")
            return self._fake_proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await dec.generate_spec(workstream, workspace)
        assert "Feature X" in captured["text"]
        assert "Do the thing" in captured["text"]
        assert "src/x.py" in captured["text"]

    @pytest.mark.anyio
    async def test_budget_none_omits_flag(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / "tasks.md").write_text("x", encoding="utf-8")
        dec = ProjectDecomposer(repo_path=temp_dir, spec_gen_budget_usd=None)
        proc = self._fake_proc()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as exec_mock:
            await dec.generate_spec(workstream, workspace)
        assert "--budget" not in list(exec_mock.call_args[0])

    @pytest.mark.anyio
    async def test_nonzero_exit_raises(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc(returncode=1, stderr=b"boom")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(DecomposerError, match="boom"):
                await dec.generate_spec(workstream, workspace)

    @pytest.mark.anyio
    async def test_zero_exit_but_no_tasks_file_raises(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)  # no tasks.md written
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(DecomposerError, match="tasks.md"):
                await dec.generate_spec(workstream, workspace)

    @pytest.mark.anyio
    async def test_spec_runner_not_found_raises(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("spec-runner")),
        ):
            with pytest.raises(DecomposerError, match="spec-runner"):
                await dec.generate_spec(workstream, workspace)

    @pytest.mark.anyio
    async def test_cancellation_terminates_subprocess(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError())
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(asyncio.CancelledError):
                await dec.generate_spec(workstream, workspace)
        proc.terminate.assert_called_once()
```

Ensure the test file imports `asyncio`, and `AsyncMock`, `MagicMock`, `patch` from `unittest.mock`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_decomposer.py::TestGenerateSpec -q`
Expected: FAIL — `generate_spec` is still sync / builds the old prompt; `spec_gen_budget_usd` param does not exist.

- [ ] **Step 3: Implement the decomposer changes**

In `maestro/decomposer.py`:

1. Imports — add at the top:

```python
import asyncio
import os
import tempfile
```

(keep `subprocess` — `_run_claude` still uses it.) Add `from maestro._vendor.obs import child_env` with the other imports.

2. Delete the entire `SPEC_GENERATION_PROMPT = """..."""` block (~line 85).

3. Delete the `_write_spec_files` method entirely (dead code — never called).

4. `__init__` (line 144) — add the budget param:

```python
    def __init__(
        self,
        repo_path: Path,
        claude_command: str = "claude",
        spec_gen_budget_usd: float | None = 1.0,
    ) -> None:
        """Initialize the decomposer.

        Args:
            repo_path: Path to the git repository.
            claude_command: Claude CLI command name.
            spec_gen_budget_usd: USD cap for `spec-runner plan --full`;
                None disables the cap.
        """
        self._repo_path = repo_path
        self._claude_command = claude_command
        self._spec_gen_budget_usd = spec_gen_budget_usd
        self._logger = logging.getLogger(__name__)
```

5. Replace the whole `generate_spec` method:

```python
    async def generate_spec(
        self,
        workstream: WorkstreamConfig,
        workspace_path: Path,
        timeout_minutes: int = 30,
    ) -> None:
        """Generate spec files by delegating to `spec-runner plan --full`.

        Writes spec/{requirements,design,tasks}.md into the workspace.
        spec-runner owns the tasks.md format (no built-in prompt copy).

        Raises:
            DecomposerError: if spec-runner is missing, exits non-zero,
                times out, or exits 0 without producing spec/tasks.md.
        """
        spec_dir = workspace_path / "spec"
        spec_dir.mkdir(exist_ok=True)

        description = (
            f"Title: {workstream.title}\n\n"
            f"Description: {workstream.description}\n\n"
            f"Scope: {', '.join(workstream.scope)}"
        )
        desc_file = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", delete=False
        )
        try:
            desc_file.write(description)
            desc_file.close()

            cmd = [
                "spec-runner",
                "plan",
                "--full",
                "--from-file",
                desc_file.name,
                "--no-branch",
                "--no-commit",
                "--no-interactive",
            ]
            if self._spec_gen_budget_usd is not None:
                cmd += ["--budget", str(self._spec_gen_budget_usd)]

            self._logger.info(
                "Generating spec for workstream '%s' via spec-runner plan --full",
                workstream.id,
            )
            await self._run_spec_runner(cmd, workspace_path, timeout_minutes)
        finally:
            Path(desc_file.name).unlink(missing_ok=True)

        tasks_path = spec_dir / "tasks.md"
        if not tasks_path.is_file():
            msg = (
                "spec-runner plan --full exited 0 but spec/tasks.md was not "
                f"created (workstream '{workstream.id}')"
            )
            raise DecomposerError(msg)
        self._logger.info("Spec generated for workstream '%s'", workstream.id)

    async def _run_spec_runner(
        self, cmd: list[str], cwd: Path, timeout_minutes: int
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

        try:
            _out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_minutes * 60
            )
        except (TimeoutError, asyncio.CancelledError) as e:
            await self._terminate(proc)
            if isinstance(e, asyncio.CancelledError):
                raise  # shutdown-driven; propagate so the caller can go READY
            msg = f"spec-runner plan --full timed out after {timeout_minutes} min"
            raise DecomposerError(msg) from e

        if proc.returncode != 0:
            stderr_text = err.decode("utf-8", "replace")[:500] if err else ""
            msg = (
                f"spec-runner plan --full failed with code "
                f"{proc.returncode}: {stderr_text}"
            )
            raise DecomposerError(msg)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate a spec-runner subprocess, escalating to kill."""
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
```

Add `import contextlib` to the imports.

6. Orchestrator call site (`maestro/orchestrator.py:334`) — add `await`:

```python
        await self._decomposer.generate_spec(workstream_config, workspace)
```

- [ ] **Step 4: Fix the orchestrator decomposer mock (keep the suite green)**

`tests/test_orchestrator.py:64-69` — the `generate_spec` mock must be async now:

```python
@pytest.fixture
def mock_decomposer() -> MagicMock:
    decomposer = MagicMock()
    decomposer.decompose = MagicMock(return_value=[])
    decomposer.generate_spec = AsyncMock()
    return decomposer
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_decomposer.py tests/test_orchestrator.py -q && uv run pytest -q`
Expected: PASS. (Orchestrator still awaits generation inline/serially — Task 3 makes it concurrent; behavior is correct, just not yet parallel.)

- [ ] **Step 6: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/decomposer.py maestro/orchestrator.py tests/test_decomposer.py tests/test_orchestrator.py
git commit -m "feat(decomposer): generate_spec delegates to spec-runner plan --full (async)

Deletes the built-in SPEC_GENERATION_PROMPT format copy and the dead
_write_spec_files parser. generate_spec is now async: it runs
spec-runner plan --full, terminates the subprocess on cancel/timeout
(no orphaned token-burning process), and fails fast if spec/tasks.md is
absent after a zero exit. Budget cap via spec_gen_budget_usd (default 1.0)."
```

---

### Task 2: `spec_gen_budget_usd` config field + CLI plumbing

**Files:**
- Modify: `maestro/models.py` (`SpecRunnerConfig`)
- Modify: `maestro/cli.py:1316` (`ProjectDecomposer(...)` construction)
- Test: `tests/test_config.py` or `tests/test_models.py` (config default), `tests/test_cli.py` (plumbing — optional, see step)

**Interfaces:**
- Consumes: `ProjectDecomposer(spec_gen_budget_usd=...)` (Task 1).
- Produces: `SpecRunnerConfig.spec_gen_budget_usd: float | None` reaching the decomposer.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py` (near other `SpecRunnerConfig` tests; if none, a new `TestSpecRunnerConfig` class):

```python
def test_spec_runner_config_budget_default() -> None:
    from maestro.models import SpecRunnerConfig

    cfg = SpecRunnerConfig()
    assert cfg.spec_gen_budget_usd == 1.0


def test_spec_runner_config_budget_none_allowed() -> None:
    from maestro.models import SpecRunnerConfig

    cfg = SpecRunnerConfig(spec_gen_budget_usd=None)
    assert cfg.spec_gen_budget_usd is None


def test_spec_runner_config_budget_rejects_negative() -> None:
    import pytest
    from pydantic import ValidationError

    from maestro.models import SpecRunnerConfig

    with pytest.raises(ValidationError):
        SpecRunnerConfig(spec_gen_budget_usd=-1.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_models.py -k budget -q`
Expected: FAIL — no such field.

- [ ] **Step 3: Implement**

`maestro/models.py`, `SpecRunnerConfig` — add after `run_lint_on_done`:

```python
    spec_gen_budget_usd: float | None = Field(
        default=1.0,
        ge=0,
        description=(
            "USD cap for `spec-runner plan --full` spec generation; "
            "None disables the cap"
        ),
    )
```

`maestro/cli.py:1316` — pass it through:

```python
        decomposer = ProjectDecomposer(
            repo_path=repo_path,
            spec_gen_budget_usd=config.spec_runner.spec_gen_budget_usd,
        )
```

(Confirm the local variable holding `OrchestratorConfig` is named `config` at that point in the orchestrate command; adjust to the actual name.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_models.py tests/test_cli.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/models.py maestro/cli.py tests/test_models.py
git commit -m "feat(config): SpecRunnerConfig.spec_gen_budget_usd plumbed to the decomposer

Per-project cap for spec-runner plan --full (default 1.0 ~= 4-10x the
measured cost; None disables). The orchestrate CLI passes it into
ProjectDecomposer."
```

---

### Task 3: Orchestrator — async background-task spec generation

**Files:**
- Modify: `maestro/orchestrator.py` (`__init__` add `_generating`; `_spawn_ready`; split `_spawn_workstream`; new `_generate_and_launch`; `_cleanup` cancels generations)
- Test: `tests/test_orchestrator.py` (concurrency behavior)

**Interfaces:**
- Consumes: `await self._decomposer.generate_spec(...)` (Task 1), `self._handle_failure` (existing, orchestrator.py:625).
- Produces: `self._generating: dict[str, asyncio.Task[None]]`; `async def _generate_and_launch(self, workstream_id: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py` (adapt fixtures to the file's existing `orchestrator`/`mock_*` fixtures):

```python
class TestBackgroundGeneration:
    @pytest.mark.anyio
    async def test_spawn_ready_does_not_block_on_generation(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """generate_spec is launched as a background task; _spawn_ready
        returns before it completes."""
        import asyncio

        gate = asyncio.Event()

        async def slow_generate(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=slow_generate)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        # generation still in flight, but _spawn_ready already returned:
        assert "z1" in orchestrator._generating
        assert not orchestrator._generating["z1"].done()
        gate.set()
        await orchestrator._generating["z1"]  # let it finish/cleanup

    @pytest.mark.anyio
    async def test_slot_accounting_counts_generating(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """max_concurrent bounds generating + running (no overspawn)."""
        import asyncio

        orchestrator._config.max_concurrent = 2
        gate = asyncio.Event()
        mock_decomposer.generate_spec = AsyncMock(side_effect=lambda *a, **k: gate.wait())
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1", "z2", "z3"])
        assert len(orchestrator._generating) == 2  # z3 held back
        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(Exception):
                await t

    @pytest.mark.anyio
    async def test_generation_failure_routes_through_handle_failure(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        from maestro.decomposer import DecomposerError

        mock_decomposer.generate_spec = AsyncMock(
            side_effect=DecomposerError("nope")
        )
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY, retry_count=0, max_retries=2)
        )
        orchestrator._handle_failure = AsyncMock()

        await orchestrator._generate_and_launch("z1")
        orchestrator._handle_failure.assert_awaited_once()
        assert "z1" not in orchestrator._generating  # slot freed in finally

    @pytest.mark.anyio
    async def test_shutdown_cancels_generation_back_to_ready(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        started = asyncio.Event()

        async def hang(*a, **k):
            started.set()
            await asyncio.sleep(3600)

        mock_decomposer.generate_spec = AsyncMock(side_effect=hang)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        await started.wait()
        await orchestrator._cleanup()
        # generation task cancelled, workstream returned to READY (no retry used)
        calls = [c.args for c in mock_db.update_workstream_status.await_args_list]
        assert any(WorkstreamStatus.READY in c for c in calls)
```

Add a `_ws(...)` helper at module scope in the test file if not present:

```python
def _ws(zid, status, *, retry_count=0, max_retries=3):
    from maestro.models import Workstream

    return Workstream(
        id=zid, title=zid, description="d", scope=["s"], branch=f"feature/{zid}",
        status=status, retry_count=retry_count, max_retries=max_retries,
    )
```

(Match `Workstream`'s actual required fields — check `maestro/models.py`; add any missing required args.)

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_orchestrator.py::TestBackgroundGeneration -q`
Expected: FAIL — `_generating` / `_generate_and_launch` do not exist; `_spawn_ready` still awaits inline.

- [ ] **Step 3: Implement**

`maestro/orchestrator.py`:

1. `__init__` — add after `self._running` (line 37):

```python
        self._generating: dict[str, asyncio.Task[None]] = {}
```

2. `_spawn_ready` (line 275) — count generating against the limit and launch background tasks instead of awaiting inline:

```python
    async def _spawn_ready(self, ready_ids: list[str]) -> None:
        """Launch background spec generation for ready workstreams up to the
        concurrency limit. Generation runs off the main loop so monitoring
        and shutdown stay responsive."""
        available = (
            self._config.max_concurrent
            - len(self._running)
            - len(self._generating)
        )
        for zid in ready_ids[:available]:
            if self._shutdown_requested:
                break
            self._generating[zid] = asyncio.create_task(
                self._generate_and_launch(zid)
            )
```

3. New `_generate_and_launch` — wrap the existing `_spawn_workstream` body with cancel/failure/finally handling:

```python
    async def _generate_and_launch(self, workstream_id: str) -> None:
        """Background task: generate the spec, then spawn `run --all`.

        - Cancellation (shutdown) → return the workstream to READY, no retry
          consumed, and propagate the cancel.
        - Any other error → _handle_failure (retry accounting).
        - The _generating slot is always freed in `finally`.
        """
        try:
            await self._spawn_workstream(workstream_id)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._db.update_workstream_status(
                    workstream_id, WorkstreamStatus.READY
                )
            raise
        except Exception as e:  # noqa: BLE001 — routed through retry accounting
            self._logger.error(
                "Spec generation failed for workstream '%s': %s",
                workstream_id,
                e,
            )
            await self._handle_failure(workstream_id, str(e))
        finally:
            self._generating.pop(workstream_id, None)
```

(`_spawn_workstream` stays as-is from Task 1 — DECOMPOSING → workspace → `await generate_spec` → config → commit → spawn `run --all` → `_running`. It no longer needs its own error handling; that moved to `_generate_and_launch`.)

4. `_cleanup` (line 701) — cancel in-flight generations FIRST (before terminating `_running`):

```python
    async def _cleanup(self) -> None:
        """Cleanup running processes and in-flight generations on shutdown."""
        for zid, task in list(self._generating.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._generating.clear()

        for zid, running in list(self._running.items()):
            # ... existing body unchanged ...
```

(Keep the rest of `_cleanup` exactly as it is; only prepend the generation-cancellation loop.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_orchestrator.py -q && uv run pytest -q`
Expected: PASS. If the shutdown test races, ensure `_generate_and_launch`'s CancelledError branch runs before `_generating.pop` (it does — pop is in `finally`, the READY update is in the `except`).

- [ ] **Step 5: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): spec generation runs as an async background task

--full is ~6 min; running it inline would serialize Mode-2 into an
~18-min queue and starve monitoring/shutdown. Generation now runs as a
per-workstream background task counted against max_concurrent (no
overspawn); failures route through _handle_failure (retry accounting),
shutdown cancels in-flight generations back to READY (no retry consumed,
subprocess terminated)."
```

---

### Task 4: Version-pin comment + golden drift test + weekly CI job

**Files:**
- Modify: `maestro/spec_runner.py` (pin comment ~line 28-31)
- Create: `tests/test_spec_runner_plan_e2e.py` (real subprocess, auto-skip)
- Modify: `.github/workflows/ci.yml` (weekly golden job)

**Interfaces:** consumes the shipped `generate_spec` (Task 1).

- [ ] **Step 1: Extend the pin comment**

`maestro/spec_runner.py` — replace the comment above `SPEC_RUNNER_REQUIRED_VERSION`:

```python
# Pinned spec-runner version. Maestro generates `spec-runner.config.yaml`,
# parses `.executor-state.{db,json}`, AND delegates spec generation to
# `spec-runner plan --full` (C4) against this version's contract: the
# `--full` / `--from-file` / `--no-interactive` flags and the
# `spec/{requirements,design,tasks}.md` output layout. This constant is a
# DOC pin — it is not asserted at runtime (a runtime version gate is a
# separate hardening ticket). Bumping requires reviewing the contract tests
# and any format changes.
SPEC_RUNNER_REQUIRED_VERSION = "2.0.0"
```

- [ ] **Step 2: Write the golden drift test (auto-skip when spec-runner absent)**

Create `tests/test_spec_runner_plan_e2e.py`:

```python
"""Golden drift test: a real `spec-runner plan --full` produces a tasks.md
that spec-runner's own parser accepts. Auto-skipped without spec-runner;
runs as a weekly CI job (plan --full spends real Claude tokens)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from maestro.decomposer import ProjectDecomposer
from maestro.models import WorkstreamConfig

pytestmark = pytest.mark.skipif(
    shutil.which("spec-runner") is None, reason="spec-runner not installed"
)


@pytest.mark.anyio
@pytest.mark.slow
async def test_plan_full_produces_parseable_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    ws = WorkstreamConfig(
        id="golden",
        title="Add a greeting helper",
        description="Add a function that returns a greeting string, with a test.",
        scope=["src/greet.py", "tests/test_greet.py"],
    )
    dec = ProjectDecomposer(repo_path=workspace, spec_gen_budget_usd=2.0)
    await dec.generate_spec(ws, workspace)

    tasks = workspace / "spec" / "tasks.md"
    assert tasks.is_file()
    # spec-runner's own parser accepts the generated tasks.md:
    result = subprocess.run(
        ["spec-runner", "task", "list", "--project-root", str(workspace)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 3: Verify it skips locally without cost, or runs if spec-runner present**

Run: `uv run pytest tests/test_spec_runner_plan_e2e.py -q -m "not slow"`
Expected: skipped/deselected (the test is `slow`; not in the default run). Confirm it does NOT execute in the normal suite.

- [ ] **Step 4: Add the weekly CI job**

`.github/workflows/ci.yml` — add a job mirroring the `arbiter-e2e` schedule pattern (`schedule: cron: "0 6 * * 1"` already exists at the top). Append:

```yaml
  spec-runner-plan-e2e:
    name: C4 golden — spec-runner plan --full (weekly)
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
      - name: Install spec-runner
        run: uv tool install spec-runner
      - name: Run golden drift test
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: uv run pytest tests/test_spec_runner_plan_e2e.py -m slow -v
```

(Confirm the workflow already has `on.schedule`; it does — the `arbiter-e2e` job uses it. Match the repo's actual uv/checkout action versions and the Claude auth mechanism spec-runner needs — mirror how `arbiter-e2e` provides credentials.)

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/spec_runner.py tests/test_spec_runner_plan_e2e.py .github/workflows/ci.yml
git commit -m "test(c4): golden drift test + weekly CI job; extend spec-runner pin comment

The version pin stays a doc constant (runtime gate deferred), so a real
weekly `spec-runner plan --full` asserting the tasks.md parses is the
sole authoring-format drift guard. Auto-skips locally; runs on the
Monday schedule only (plan --full spends real tokens)."
```

---

### Task 5: Docs, TODO tick, final gates

**Files:**
- Modify: `CLAUDE.md` (decomposer.py architecture bullet)
- Modify: `TODO.md` (C4 / decomposer-delegation entry if present)

- [ ] **Step 1: CLAUDE.md**

Update the `decomposer.py` architecture bullet:

```markdown
- **decomposer.py**: Project decomposition via Claude CLI into workstreams (`decompose`) + async spec generation delegated to `spec-runner plan --full` (`generate_spec` — spec-runner owns the tasks.md format; runs as a background task in the orchestrator, budget-capped via `SpecRunnerConfig.spec_gen_budget_usd`)
```

- [ ] **Step 2: TODO.md**

If a C4 / "decomposer delegation" / "consolidation-ADR format dup" item exists, tick it `[x]` with `(closed by feat/c4-decomposer-delegation)`. If none exists, add a one-line done entry under an appropriate section. Do not invent unrelated items.

- [ ] **Step 3: Final gates + smoke**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
git grep -n "SPEC_GENERATION_PROMPT" || echo "SPEC_GENERATION_PROMPT gone (good)"
git grep -n "_write_spec_files" || echo "_write_spec_files gone (good)"
git status --short
```

Expected: suite green (~1470); pyrefly clean; ruff clean; both grep guards report the symbols are gone.

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: C4 decomposer delegation shipped — spec-runner owns the tasks.md format"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final whole-branch review)

```bash
git push -u origin feat/c4-decomposer-delegation
gh pr create --title "feat(decomposer): delegate spec generation to spec-runner plan --full (C4)" --body "$(cat <<'EOF'
## Summary
- `generate_spec` delegates to `spec-runner plan --full` — deletes the built-in `SPEC_GENERATION_PROMPT` format copy (spec-runner is the sole tasks.md format owner) and the dead `_write_spec_files` parser
- Delegation mode chosen by measurement: `--full` (3 calls, ~6 min) is the only clean path — `--gated --stage tasks` needs an approval chain, `--stage tasks` won't write the file. See the spec.
- Because `--full` is ~6 min, spec generation now runs as an async per-workstream **background task** (was a blocking inline call): counted against `max_concurrent` (no overspawn), so Mode-2 stays a parallel pipeline instead of an ~18-min serial queue
- Cancellation-safe: shutdown cancels in-flight generations, terminates the spec-runner subprocess (no orphan burning tokens), returns the workstream to READY (no retry consumed); real failures route through `_handle_failure` (retry accounting)
- Fail-fast: zero exit without `spec/tasks.md` → `DecomposerError`
- Budget cap `SpecRunnerConfig.spec_gen_budget_usd` (default 1.0; None disables)
- Version pin stays a doc constant; a weekly golden CI job (`spec-runner plan --full` → parseable tasks.md) is the drift guard

Spec: docs/superpowers/specs/2026-07-06-c4-decomposer-delegation-design.md

## Test plan
- [ ] Full suite green; pyrefly + ruff clean; SPEC_GENERATION_PROMPT / _write_spec_files gone
- [ ] Decomposer: command shape, description fields, budget on/off, non-zero exit, missing-tasks.md, spec-runner-absent, cancellation-terminates-subprocess
- [ ] Orchestrator: background launch (no block), slot accounting (no overspawn), failure→_handle_failure, shutdown→READY
- [ ] Golden weekly job wired (skips locally)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: §1 async generate_spec → Task 1; §1b concurrency → Task 3; §2 remove dup → Task 1; §3 dead code → Task 1; §4 budget → Tasks 1+2; §5 version pin → Task 4; §6 backward-compat → Tasks 1+3; testing (decomposer/orchestration/golden) → Tasks 1/3/4; docs → Task 5.
- Sequencing keeps the suite green: Task 1 makes `generate_spec` async AND updates the single call site to `await` + the orchestrator mock to `AsyncMock` (serial-but-working); Task 3 introduces the background-task concurrency. No task leaves the orchestrator calling a coroutine without awaiting it.
- Type consistency: `async def generate_spec(workstream, workspace_path, timeout_minutes=30)` (Task 1) — Task 3 calls it via the unchanged `_spawn_workstream` body; `spec_gen_budget_usd: float | None` identical in decomposer ctor (Task 1), config field (Task 2), and cli plumbing (Task 2); `_generating: dict[str, asyncio.Task[None]]` and `_generate_and_launch(workstream_id: str)` consistent across Task 3.
- `_terminate`/`_run_spec_runner` are private decomposer helpers introduced in Task 1 and referenced only there.
- Placeholder scan: `timeout=...` appears only in prose descriptions resolved by adjacent concrete values; all code steps carry full bodies. The cli.py `config` variable name and the Workstream test-helper required fields are flagged for the implementer to confirm against the actual code (not placeholders — verification notes).
