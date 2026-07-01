# D2 + D1: AgentType Gate & Routed-Model Passthrough — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the arbiter route to any harness that has a registered spawner (D2), and execute the exact model the arbiter routed to (D1), instead of the closed `AgentType` enum gating spawns and the model being dropped.

**Architecture:** Two changes on the `Scheduler._spawn_task` path in `maestro/scheduler.py`. D2 replaces `AgentType(harness)` enum validation with an explicit `"auto"` check + `self._spawners` membership check (HOLD/refuse semantics preserved). D1 adds an optional `model` param to `spawn()`, threads the routed model (`model_of_agent_id(routed_agent_type)`) into it, and has model-aware spawners resolve `routed > env > default` while emitting a trace-correlated `agent.model_resolved` log.

**Tech Stack:** Python 3.12+, uv, pytest + anyio, pyrefly, ruff, structlog (vendored `maestro._vendor.obs`).

**Spec:** `docs/superpowers/specs/2026-07-01-d2-d1-gate-and-model-passthrough-design.md`

## Global Constraints

- Package manager: **uv only**. Run tools via `uv run <tool>`. Never `pip`.
- Line length: **88** chars. Type hints required. Public APIs need docstrings.
- After every task: `uv run pytest`, `uv run pyrefly check`, `uv run ruff check .` all green.
- Precedence for model resolution is exactly **routed > env > default** (verbatim from spec).
- `AgentType` enum is **not** modified — it stays the type of `task.agent_type` and holds the `AUTO`/`ANNOUNCE` sentinels.
- Commit after each task. Work on branch `adr-eco-003/maestro-d2-d1` (already checked out).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `maestro/models.py` | Data models + agent-id helpers | Add `model_of_agent_id` |
| `maestro/spawners/base.py` | `AgentSpawner` ABC | Add `model` param to `spawn()` |
| `maestro/scheduler.py` | `SpawnerProtocol` + `_spawn_task` | Protocol param; D2 gate; D1 wiring |
| `maestro/spawners/claude_code.py` | Claude Code spawner | Accept `model`; resolve; log; docstring |
| `maestro/spawners/codex.py` | Codex spawner | Accept `model`; resolve; log; docstring |
| `maestro/spawners/aider.py`, `announce.py` | Other spawners | Accept `model`, ignore |
| `tests/test_models.py` | Helper tests | `model_of_agent_id` cases |
| `tests/test_spawners.py` | Spawner tests | D1 matrix + source-log |
| `tests/test_scheduler.py` | Scheduler tests + `MockSpawner` | `model` capture; D1 wiring; D2 gate |
| `CHANGELOG.md` | Changelog | Entry for D2/D1 + env-semantics change |

---

## Task 1: `model_of_agent_id` helper

**Files:**
- Modify: `maestro/models.py` (add function after `harness_of_agent_id`, ~line 105)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `model_of_agent_id(agent_id: str) -> str | None` — the substring right of the first `@`, or `None` if there is no `@`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
class TestModelOfAgentId:
    """Tests for model_of_agent_id (2026-06-19 <harness>@<model> convention)."""

    def test_extracts_model_after_at(self) -> None:
        from maestro.models import model_of_agent_id

        assert model_of_agent_id("claude_code@claude-opus-4-8") == "claude-opus-4-8"
        assert model_of_agent_id("codex_cli@gpt-5.5") == "gpt-5.5"

    def test_none_when_no_at(self) -> None:
        from maestro.models import model_of_agent_id

        assert model_of_agent_id("claude_code") is None
        assert model_of_agent_id("aider") is None

    def test_none_when_empty(self) -> None:
        from maestro.models import model_of_agent_id

        assert model_of_agent_id("") is None

    def test_splits_on_first_at(self) -> None:
        from maestro.models import model_of_agent_id

        assert model_of_agent_id("ollama@qwen2.5:14b@x") == "qwen2.5:14b@x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py::TestModelOfAgentId -v`
Expected: FAIL with `ImportError: cannot import name 'model_of_agent_id'`.

- [ ] **Step 3: Write minimal implementation**

Add to `maestro/models.py` immediately after the `harness_of_agent_id` function:

```python
def model_of_agent_id(agent_id: str) -> str | None:
    """Recover the model from an arbiter agent id, or ``None`` if absent.

    Symmetric with :func:`harness_of_agent_id`: where that returns the part
    left of the first ``@``, this returns the part right of it. A plain harness
    id with no ``@`` (the pre-change format and static/advisory routing) carries
    no model, so this returns ``None`` and the spawner falls back to its
    env/default model.

    Examples:
        ``"claude_code@claude-opus-4-8"`` -> ``"claude-opus-4-8"``
        ``"claude_code"`` -> ``None``
        ``"ollama@qwen2.5:14b@x"`` -> ``"qwen2.5:14b@x"`` (split on first ``@``)
    """
    _harness, sep, model = agent_id.partition("@")
    return model if sep else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py::TestModelOfAgentId -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff format maestro/models.py tests/test_models.py
uv run ruff check maestro/models.py tests/test_models.py
uv run pyrefly check
git add maestro/models.py tests/test_models.py
git commit -m "feat(models): add model_of_agent_id helper (D1)"
```

---

## Task 2: Thread `model` through spawners + resolve in claude_code/codex

**Files:**
- Modify: `maestro/spawners/base.py` (`spawn` ABC signature + docstring)
- Modify: `maestro/spawners/claude_code.py` (accept `model`; resolve; log; docstring)
- Modify: `maestro/spawners/codex.py` (accept `model`; resolve; log; docstring)
- Modify: `maestro/spawners/aider.py`, `maestro/spawners/announce.py` (accept `model`, ignore)
- Modify: `tests/test_scheduler.py` (`MockSpawner.spawn` + `FailingSpawner.spawn` accept `model`, capture)
- Test: `tests/test_spawners.py`

**Interfaces:**
- Produces: `AgentSpawner.spawn(task, context, workdir, log_file, retry_context="", *, model: str | None = None) -> Popen[bytes]`.
- Produces: model-aware spawners emit structlog event `agent.model_resolved` with keys `harness`, `model`, `source ∈ {"routed","env","default"}`.
- Consumes: nothing from Task 1 (this task is spawner-local).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_spawners.py` inside `class TestClaudeCodeSpawner` (mirror the existing `test_spawn_model_override_from_env`; the `@patch` decorators and fixtures are identical):

```python
    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_uses_routed_model(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """An explicit routed model wins and reaches --model."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42
        workdir = Path(sample_task.workdir)

        claude_spawner.spawn(
            sample_task, "", workdir, temp_dir / "t.log", model="claude-opus-4-8"
        )

        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_routed_model_beats_env(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Precedence routed > env: routed model overrides MAESTRO_CLAUDE_MODEL."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42
        workdir = Path(sample_task.workdir)

        with patch.dict(os.environ, {"MAESTRO_CLAUDE_MODEL": "env-model"}):
            claude_spawner.spawn(
                sample_task, "", workdir, temp_dir / "t.log", model="routed-model"
            )

        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "routed-model"

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_emits_model_resolved_source(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """agent.model_resolved reports the correct source for each origin."""
        from structlog.testing import capture_logs

        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42
        workdir = Path(sample_task.workdir)

        with capture_logs() as logs:
            claude_spawner.spawn(
                sample_task, "", workdir, temp_dir / "t.log", model="routed-x"
            )
        ev = next(e for e in logs if e["event"] == "agent.model_resolved")
        assert ev["source"] == "routed"
        assert ev["model"] == "routed-x"

        with capture_logs() as logs, patch.dict(
            os.environ, {"MAESTRO_CLAUDE_MODEL": "env-y"}
        ):
            claude_spawner.spawn(sample_task, "", workdir, temp_dir / "t.log")
        ev = next(e for e in logs if e["event"] == "agent.model_resolved")
        assert ev["source"] == "env"
        assert ev["model"] == "env-y"

        with capture_logs() as logs, patch.dict(os.environ, {}, clear=True):
            claude_spawner.spawn(sample_task, "", workdir, temp_dir / "t.log")
        ev = next(e for e in logs if e["event"] == "agent.model_resolved")
        assert ev["source"] == "default"
        assert ev["model"] == DEFAULT_CLAUDE_MODEL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spawners.py -k "routed_model or model_resolved" -v`
Expected: FAIL — `spawn()` has no `model` parameter (`TypeError: spawn() got an unexpected keyword argument 'model'`).

- [ ] **Step 3: Update the ABC signature**

In `maestro/spawners/base.py`, change the abstract `spawn` signature and docstring:

```python
    @abstractmethod
    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> Popen[bytes]:
        """Spawn agent process.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            retry_context: Error context from previous failed attempt.
            model: Routed model from the arbiter (``model_of_agent_id`` of
                ``routed_agent_type``). ``None`` in scheduler mode; model-aware
                spawners then fall back to env/default. Ignored by spawners with
                no model concept (aider, announce).

        Returns:
            Subprocess handle for monitoring.
        """
        ...
```

- [ ] **Step 4: Implement resolution in claude_code**

In `maestro/spawners/claude_code.py`: add the obs import near the top imports:

```python
from maestro._vendor import obs
```

Add a module-level logger after `DEFAULT_CLAUDE_MODEL`:

```python
_obs_log = obs.get_logger("maestro.spawners.claude_code")
```

Change the `spawn` signature to match the ABC (add `*, model: str | None = None`) and replace the model line (`model = os.environ.get("MAESTRO_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL`) with:

```python
        if model:
            resolved, source = model, "routed"
        elif os.environ.get("MAESTRO_CLAUDE_MODEL"):
            resolved, source = os.environ["MAESTRO_CLAUDE_MODEL"], "env"
        else:
            resolved, source = DEFAULT_CLAUDE_MODEL, "default"
        _obs_log.info(
            "agent.model_resolved",
            harness="claude_code",
            model=resolved,
            source=source,
        )
```

Then use `resolved` where the command is built (`"--model", model,` → `"--model", resolved,`).

- [ ] **Step 5: Implement resolution in codex**

In `maestro/spawners/codex.py`: add `from maestro._vendor import obs` and
`_obs_log = obs.get_logger("maestro.spawners.codex")` after `DEFAULT_CODEX_MODEL`.
Change the `spawn` signature to add `*, model: str | None = None`, and replace
`model = os.environ.get("MAESTRO_CODEX_MODEL") or DEFAULT_CODEX_MODEL` with:

```python
        if model:
            resolved, source = model, "routed"
        elif os.environ.get("MAESTRO_CODEX_MODEL"):
            resolved, source = os.environ["MAESTRO_CODEX_MODEL"], "env"
        else:
            resolved, source = DEFAULT_CODEX_MODEL, "default"
        _obs_log.info(
            "agent.model_resolved",
            harness="codex_cli",
            model=resolved,
            source=source,
        )
```

Use `resolved` in the argv where `"-m", model,` is built (`"-m", resolved,`).

- [ ] **Step 6: Update aider, announce, and the docstrings**

In `maestro/spawners/aider.py` and `maestro/spawners/announce.py`, add
`*, model: str | None = None` to each `spawn` signature (they ignore it; a
one-line note in the docstring: `model: accepted for interface parity; unused
(no model concept).`).

Fix the misleading docstrings (spec review P2): in `claude_code.py` change the
class docstring line "override via ``MAESTRO_CLAUDE_MODEL``" to
"routed model wins; ``MAESTRO_CLAUDE_MODEL`` is the fallback when routing
supplies none"; make the equivalent edit in `codex.py`.

- [ ] **Step 7: Update the test doubles**

In `tests/test_scheduler.py`, update `MockSpawner.spawn` and `FailingSpawner.spawn`
to accept the new parameter. For `MockSpawner`, capture it:

Add to `MockSpawner.__init__`: `self._spawned_models: list[str | None] = []`.
Add a property:

```python
    @property
    def spawned_models(self) -> list[str | None]:
        return self._spawned_models
```

Change `MockSpawner.spawn` signature to:

```python
    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> subprocess.Popen[bytes]:
```

and add `self._spawned_models.append(model)` next to the other `self._spawned_*` appends.

Change `FailingSpawner.spawn` signature the same way (add `*, model: str | None = None`).

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_spawners.py -v`
Expected: PASS, including the three new tests. Also run the existing
`test_spawn_creates_process_with_correct_args` and `test_spawn_model_override_from_env`
— both still PASS (default + env paths unchanged).

- [ ] **Step 9: Lint + typecheck + commit**

```bash
uv run ruff format maestro/spawners/ tests/test_spawners.py tests/test_scheduler.py
uv run ruff check maestro/spawners/ tests/test_spawners.py tests/test_scheduler.py
uv run pyrefly check
git add maestro/spawners/ tests/test_spawners.py tests/test_scheduler.py
git commit -m "feat(spawners): accept routed model, resolve routed>env>default, log source (D1)"
```

---

## Task 3: Scheduler passes the routed model into `spawn()`

**Files:**
- Modify: `maestro/scheduler.py` (`SpawnerProtocol.spawn` signature; import; spawn call ~877)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `model_of_agent_id` (Task 1); `MockSpawner.spawned_models` (Task 2).
- Produces: at the spawn call site, `spawner.spawn(..., model=routed_model)` where
  `routed_model = model_of_agent_id(task.routed_agent_type)` or `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scheduler.py`:

```python
class TestSchedulerModelPassthrough:
    """D1: the arbiter-routed model reaches the spawner."""

    @pytest.mark.anyio
    async def test_routed_model_passed_to_spawner(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from unittest.mock import AsyncMock

        from maestro.models import (
            ArbiterMode,
            RouteAction,
            RouteDecision,
            Task,
            TaskConfig,
        )

        db = await create_database(temp_db_path)
        try:
            config = TaskConfig(id="t", title="T", prompt="do it")
            await db.create_task(Task.from_config(config, str(temp_db_path.parent)))

            spawner = MockSpawner("claude_code")
            routing = AsyncMock()
            routing.route.return_value = RouteDecision(
                action=RouteAction.ASSIGN,
                chosen_agent="claude_code@claude-opus-4-8",
                decision_id="d1",
                reason="test",
            )
            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={"claude_code": spawner},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )

            await scheduler._spawn_ready_tasks(["t"])

            assert spawner.spawn_count == 1
            assert spawner.spawned_models == ["claude-opus-4-8"]
        finally:
            await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler.py::TestSchedulerModelPassthrough -v`
Expected: FAIL — `spawner.spawned_models == [None]` (routed model not yet passed).

- [ ] **Step 3: Wire the scheduler**

In `maestro/scheduler.py`, add `model_of_agent_id` to the existing
`from maestro.models import (... harness_of_agent_id ...)` import.

Update `SpawnerProtocol.spawn` (around line 72) to match the ABC — append
`*, model: str | None = None` before `-> Popen[bytes]`.

At the spawn call (currently
`process = spawner.spawn(task, context, workdir, log_file, retry_context)`),
compute and pass the routed model:

```python
            routed_model = (
                model_of_agent_id(task.routed_agent_type)
                if task.routed_agent_type
                else None
            )
            process = spawner.spawn(
                task, context, workdir, log_file, retry_context, model=routed_model
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler.py::TestSchedulerModelPassthrough -v`
Expected: PASS.

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff format maestro/scheduler.py tests/test_scheduler.py
uv run ruff check maestro/scheduler.py tests/test_scheduler.py
uv run pyrefly check
git add maestro/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): pass routed model from routed_agent_type into spawn (D1)"
```

---

## Task 4: Open the D2 gate (registry membership, not enum)

**Files:**
- Modify: `maestro/scheduler.py` (`_spawn_task` gate, ~751-778)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `self._spawners: dict[str, SpawnerProtocol]`; `harness_of_agent_id`;
  `AgentType.AUTO`; `EventType.ARBITER_ROUTE_HOLD`.
- Produces: no new public interface — behavior change only (non-enum harness with a
  registered spawner now spawns instead of HOLD).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scheduler.py`:

```python
class TestSchedulerD2Gate:
    """D2: routing gate validates against the spawner registry, not the enum."""

    async def _run(
        self, temp_db_path: Path, temp_dir: Path, spawners: dict, chosen: str
    ):
        from unittest.mock import AsyncMock

        from maestro.models import ArbiterMode, RouteAction, RouteDecision, Task, TaskConfig

        db = await create_database(temp_db_path)
        config = TaskConfig(id="t", title="T", prompt="do it")
        await db.create_task(Task.from_config(config, str(temp_db_path.parent)))
        routing = AsyncMock()
        routing.route.return_value = RouteDecision(
            action=RouteAction.ASSIGN, chosen_agent=chosen, decision_id="d", reason="t"
        )
        scheduler = Scheduler(
            db=db,
            dag=DAG([config]),
            spawners=spawners,
            config=SchedulerConfig(log_dir=temp_dir / "logs"),
            routing=routing,
            arbiter_mode=ArbiterMode.AUTHORITATIVE,
        )
        await scheduler._spawn_ready_tasks(["t"])
        return db, scheduler

    @pytest.mark.anyio
    async def test_non_enum_harness_with_spawner_spawns(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        """PROOF OF D2: 'opencode' is not an AgentType member, but has a spawner."""
        spawner = MockSpawner("opencode")
        db, _ = await self._run(
            temp_db_path, temp_dir, {"opencode": spawner}, "opencode@glm-5.1"
        )
        try:
            assert spawner.spawn_count == 1  # previously HOLD via ValueError
            assert spawner.spawned_models == ["glm-5.1"]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unknown_harness_holds(
        self, temp_db_path: Path, temp_dir: Path, caplog
    ) -> None:
        """A harness with no registered spawner → HOLD (unknown_agent), stays READY."""
        import logging

        from maestro.models import TaskStatus

        spawner = MockSpawner("claude_code")
        with caplog.at_level(logging.WARNING):
            db, _ = await self._run(
                temp_db_path, temp_dir, {"claude_code": spawner}, "ghost@x"
            )
        try:
            assert spawner.spawn_count == 0
            assert "unknown agent" in caplog.text
            task = await db.get_task("t")
            assert task.status == TaskStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_auto_sentinel_refused(
        self, temp_db_path: Path, temp_dir: Path, caplog
    ) -> None:
        """chosen_agent 'auto' → refuse (auto_not_resolved), not spawned."""
        import logging

        spawner = MockSpawner("claude_code")
        with caplog.at_level(logging.ERROR):
            db, _ = await self._run(
                temp_db_path, temp_dir, {"claude_code": spawner}, "auto"
            )
        try:
            assert spawner.spawn_count == 0
            assert "refusing to spawn" in caplog.text
        finally:
            await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scheduler.py::TestSchedulerD2Gate -v`
Expected: `test_non_enum_harness_with_spawner_spawns` FAILS (spawn_count 0 — enum
gate HOLDs `opencode`). The other two may already pass (unknown/auto); all three
must pass after Step 3.

- [ ] **Step 3: Replace the enum gate**

In `maestro/scheduler.py`, replace the block that starts at
`try:\n    chosen = AgentType(harness_of_agent_id(decision.chosen_agent))`
and includes both the `except ValueError:` HOLD and the separate
`if chosen is AgentType.AUTO:` refuse block, with:

```python
        harness = harness_of_agent_id(decision.chosen_agent)
        if harness == AgentType.AUTO.value:
            logger.error(
                "routing returned AUTO for task %s — refusing to spawn", task_id
            )
            if self._hold_throttle.should_log(task_id, "auto_not_resolved"):
                self._emit_event(
                    EventType.ARBITER_ROUTE_HOLD,
                    {"task_id": task_id, "reason": "auto_not_resolved"},
                )
            return False
        if harness not in self._spawners:
            logger.warning(
                "arbiter chose unknown agent %r for task %s — HOLD",
                decision.chosen_agent,
                task_id,
            )
            if self._hold_throttle.should_log(task_id, "unknown_agent"):
                self._emit_event(
                    EventType.ARBITER_ROUTE_HOLD,
                    {"task_id": task_id, "reason": "unknown_agent"},
                )
            return False
```

(The downstream `spawner_key = harness_of_agent_id(...)` / `self._spawners.get(...)`
lookup and `is_available()` check are unchanged. The local `chosen` variable is no
longer referenced anywhere and is fully removed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_scheduler.py::TestSchedulerD2Gate -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff format maestro/scheduler.py tests/test_scheduler.py
uv run ruff check maestro/scheduler.py tests/test_scheduler.py
uv run pyrefly check
git add maestro/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): open AgentType gate — validate harness via registry (D2)"
```

---

## Task 5: CHANGELOG + full regression

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the changelog entry**

Insert a new section under the top `# Changelog` heading:

```markdown
## Unreleased

### Changed
- **Routing (D2):** the arbiter can now route to any harness that has a
  registered spawner; the closed `AgentType` enum no longer gates spawns
  (`scheduler.py`). Unknown harness → HOLD (`unknown_agent`); `auto` → refuse
  (`auto_not_resolved`) — semantics unchanged.
- **Model execution (D1):** the arbiter-routed model (`<harness>@<model>`) is now
  passed into `spawn()` and executed. Precedence is **routed > env > default**.
  **Behaviour change:** `MAESTRO_CLAUDE_MODEL` / `MAESTRO_CODEX_MODEL` are now a
  *fallback* used only when routing supplies no model — they no longer override a
  routed decision. Each spawn emits an `agent.model_resolved {harness, model,
  source}` log for observability. Catalog-membership validation of the routed
  model is deferred to ADR-ECO-003 AI#4.
```

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest`
Expected: all green (no regressions).

Run: `uv run pyrefly check`
Expected: clean.

Run: `uv run ruff check .`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record D2 gate opening + D1 model passthrough"
```

---

## Self-Review

**1. Spec coverage:**
- D2 registry gate → Task 4. ✓
- `auto` refuse / unknown HOLD semantics preserved → Task 4 tests 2-3. ✓
- D2 proof (non-enum harness spawns) → Task 4 test 1. ✓
- D1 `model_of_agent_id` → Task 1. ✓
- D1 spawn param + routed>env>default → Task 2. ✓
- D1 scheduler wiring → Task 3. ✓
- Observability `agent.model_resolved` + source (review P1/P3) → Task 2 step 4-5, test step 1. ✓
- Env-semantics docstrings + changelog (review P2) → Task 2 step 6, Task 5. ✓
- Built-in-without-spawner HOLD note (review P4) → covered by Task 4 gate; documented in spec, no separate code. ✓
- AgentType enum unchanged → no task touches it (constraint). ✓
- Deferred (AI#4 catalog validation) → noted in Task 5 changelog, not implemented. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows the assertion. ✓

**3. Type consistency:** `model: str | None = None` identical across ABC (Task 2), `SpawnerProtocol` (Task 3), all spawners (Task 2), and test doubles (Task 2). `model_of_agent_id -> str | None` defined Task 1, consumed Task 3. `spawned_models: list[str | None]` defined Task 2, asserted Tasks 3-4. ✓
