# cost-from-log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface opencode's self-reported `part.cost` into `TaskCost.reported_cost_usd`, `TaskOutcome.cost_usd`, and cost summaries, and dispatch the log parser by the EFFECTIVE harness so routed tasks stop losing token/cost telemetry.

**Architecture:** `TokenUsage` gains `cost_usd: float | None`; `parse_opencode_log` fills it by summing per-step `part.cost`. `TaskCost` gains a `reported_cost_usd` nullable column (migration #4, LABS-85 journal pattern). `Scheduler._record_cost` resolves the effective harness (`routed_agent_type` wins) before parser dispatch. `Scheduler._build_outcome` reports `sum(effective costs)` where effective = reported ?? (estimated if priced) ?? unknown. Summaries use COALESCE (non-nullable floats).

**Tech Stack:** Python 3.12+, uv, pytest (anyio for async), pyrefly, ruff, aiosqlite. Spec: `docs/superpowers/specs/2026-07-05-cost-from-log-design.md`.

## Global Constraints

- Package management: `uv` only. Tests: `uv run pytest`. Types: `uv run pyrefly check`. Lint: `uv run ruff format .` + `uv run ruff check .`. Line length 88. Async tests: `@pytest.mark.anyio`.
- `cost_usd=None` means "unknown" and must NEVER collapse to 0.0 at the arbiter-outcome boundary. Summaries stay non-nullable floats (COALESCE to estimate).
- Numeric guard for `part.cost`: `isinstance(v, (int, float)) and not isinstance(v, bool)` — JSON `true` must NOT leak in as $1.00.
- Real-fixture cost literal (jq-derived, independent of the parser): 0.0170512 + 0.00359536 = **0.02064656**.
- Migration #4 follows the LABS-85 pattern exactly: one tuple in `ordered`, one idempotent method via `PRAGMA table_info`. Never reorder existing migrations.
- Reported cost source this iteration: opencode ONLY. claude/codex/aider parsers keep `cost_usd=None`.
- Branch: `feat/cost-from-log` (already exists, spec committed). Full suite (~1360) green at every commit.

---

### Task 1: `TokenUsage.cost_usd` + cost extraction in `parse_opencode_log`

**Files:**
- Modify: `maestro/cost_tracker.py` (TokenUsage dataclass ~line 55; `parse_opencode_log` lines 147-193)
- Test: `tests/test_cost_tracker.py` (extend `TestOpencodeLogParsing`)

**Interfaces:**
- Produces: `TokenUsage.cost_usd: float | None = None` (None = agent reported no cost). `parse_opencode_log` fills it by summing numeric `part.cost` across `step_finish` events. Tasks 2-5 rely on `usage.cost_usd`.

- [ ] **Step 1: Write the failing tests**

Add to `TestOpencodeLogParsing` in `tests/test_cost_tracker.py`:

```python
    def test_parse_real_fixture_sums_cost(self) -> None:
        """Real captured run: per-step part.cost summed across step_finish.

        Literal computed with jq independently of the parser:
        0.0170512 + 0.00359536 = 0.02064656. Per-step semantics proven by
        the same fixture argument as tokens: step 2's cost (0.00359536) is
        LESS than step 1's (0.0170512) — impossible for a cumulative counter.
        """
        usage = parse_opencode_log(self.FIXTURE.read_text(encoding="utf-8"))
        assert usage.cost_usd == pytest.approx(0.02064656, rel=1e-9)

    def test_no_cost_reported_is_none_not_zero(self) -> None:
        """A run whose steps carry no cost → cost_usd is None (unknown)."""
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.cost_usd is None
        assert usage.input_tokens == 10  # tokens still parsed

    def test_null_cost_skipped(self) -> None:
        log = (
            '{"type": "step_finish", "part": {"cost": null, "tokens": '
            '{"input": 1, "output": 1}}}\n'
        )
        assert parse_opencode_log(log).cost_usd is None

    def test_bool_cost_ignored(self) -> None:
        """bool is an int subclass in Python; JSON true must not become $1."""
        log = (
            '{"type": "step_finish", "part": {"cost": true, "tokens": '
            '{"input": 1, "output": 1}}}\n'
        )
        assert parse_opencode_log(log).cost_usd is None

    def test_partial_cost_sums_available_steps(self) -> None:
        """One step with cost, one without → total is the one reported value."""
        log = (
            '{"type": "step_finish", "part": {"cost": 0.01, "tokens": '
            '{"input": 10, "output": 2}}}\n'
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 5, "output": 1}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.cost_usd == pytest.approx(0.01)
        assert usage.input_tokens == 15

    def test_cost_only_step_without_tokens_counted(self) -> None:
        """A step_finish with cost but no tokens dict still contributes cost."""
        log = '{"type": "step_finish", "part": {"cost": 0.02}}\n'
        usage = parse_opencode_log(log)
        assert usage.cost_usd == pytest.approx(0.02)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cost_tracker.py::TestOpencodeLogParsing -q`
Expected: FAIL — `TokenUsage` has no attribute `cost_usd`.

- [ ] **Step 3: Implement**

In `maestro/cost_tracker.py`, extend the dataclass:

```python
@dataclass
class TokenUsage:
    """Parsed token usage from agent logs."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    """Agent-reported cost in USD (e.g. opencode's per-step ``part.cost``).

    None means the agent did not report a cost — never collapse to 0.0.
    Only the opencode parser fills this today; claude/codex/aider logs are
    priced from PRICING downstream.
    """
```

Rewrite `parse_opencode_log`'s docstring middle paragraph and loop body (the token arithmetic is unchanged; cost extraction is added on `part`, BEFORE the tokens gate, so a cost-only step still counts — and `saw_step_finish` moves up next to the `part` check accordingly):

```python
    ``part.tokens.cache.read`` / ``part.tokens.cache.write`` are intentionally
    dropped: Maestro never computes opencode cost from tokens, so cache reads
    are never billed at input price. ``part.cost`` IS extracted (summed
    per-step, same fixture-proven semantics) into ``TokenUsage.cost_usd`` —
    opencode's own number already prices cache correctly.
```

```python
    usage = TokenUsage()
    saw_step_finish = False
    saw_cost = False
    cost_total = 0.0
    for raw_line in log_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        saw_step_finish = True
        cost = part.get("cost")
        # bool is an int subclass: JSON true must not leak in as $1.00.
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            saw_cost = True
            cost_total += float(cost)
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        usage.input_tokens += int(tokens.get("input") or 0)
        usage.output_tokens += int(tokens.get("output") or 0) + int(
            tokens.get("reasoning") or 0
        )
    if saw_cost:
        usage.cost_usd = cost_total
    if log_content.strip() and not saw_step_finish:
        # Format-drift canary: opencode renaming/removing step_finish would
        # otherwise zero out token tracking with no signal at all.
        logger.debug("opencode log had no step_finish events — format drift?")
    return usage
```

Also update the Returns: line of the docstring to mention cost.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost_tracker.py -q`
Expected: PASS (all pre-existing parser tests too — `TokenUsage()` equality still holds because `cost_usd` defaults to None on both sides).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/cost_tracker.py tests/test_cost_tracker.py
git commit -m "feat(cost): parse_opencode_log extracts per-step part.cost into TokenUsage.cost_usd

Per-step semantics proven by the fixture (step 2 cost < step 1 — a
cumulative counter cannot decrease). bool guard: JSON true is not \$1.
None (never 0.0) when no step reports a numeric cost."
```

---

### Task 2: `TaskCost.reported_cost_usd` + DB migration #4

**Files:**
- Modify: `maestro/models.py` (TaskCost, ~line 854)
- Modify: `maestro/database.py` (CREATE TABLE task_costs DDL ~line 116; `ordered` migrations list ~line 378; new `_migrate_task_costs_reported_cost` method after `_migrate_tasks_arbiter_routing`; `save_task_cost` INSERT ~line 1537; `_row_to_task_cost` ~line 260)
- Modify: `maestro/cost_tracker.py` (`create_task_cost` ~line 264; `parse_and_create_cost` gate ~line 304)
- Test: `tests/test_database.py` (migration + round-trip), `tests/test_cost_tracker.py` (create/gate)

**Interfaces:**
- Consumes: `TokenUsage.cost_usd` (Task 1).
- Produces: `TaskCost.reported_cost_usd: float | None` (Field default=None, ge=0.0) persisted through save/get; `create_task_cost` fills it from `usage.cost_usd`; `parse_and_create_cost` returns a row when tokens are zero but cost is reported. Tasks 3-5 rely on the field round-tripping.

- [ ] **Step 1: Write the failing tests**

In `tests/test_database.py` (near the existing schema-migration tests; the file already imports `Database`, `TaskCost`, `AgentType`):

```python
@pytest.mark.anyio
async def test_migration_4_adds_reported_cost_column(tmp_path) -> None:
    """A pre-#4 database gains reported_cost_usd on connect + journal row."""
    import aiosqlite

    db_path = tmp_path / "old.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE task_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL DEFAULT 0.0,
                attempt INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()

    db = Database(db_path)
    await db.connect()
    try:
        assert db._connection is not None
        cursor = await db._connection.execute("PRAGMA table_info(task_costs)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "reported_cost_usd" in columns
        cursor = await db._connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 4"
        )
        row = await cursor.fetchone()
        assert row is not None and row["name"] == "cost_from_log_reported_cost"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_task_cost_reported_cost_round_trip(tmp_path) -> None:
    """reported_cost_usd survives save/get for both a value and None."""
    from maestro.models import Task, TaskStatus

    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.OPENCODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)
        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=100,
                output_tokens=20,
                estimated_cost_usd=0.0,
                reported_cost_usd=0.0123,
                attempt=1,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=50,
                output_tokens=10,
                estimated_cost_usd=0.0,
                attempt=2,
            )
        )
        rows = await db.get_task_costs("t1")
        assert rows[0].reported_cost_usd == pytest.approx(0.0123)
        assert rows[1].reported_cost_usd is None
    finally:
        await db.close()
```

In `tests/test_cost_tracker.py` (`TestCreateTaskCost` and `TestParseAndCreateCost` areas):

```python
    def test_create_task_cost_carries_reported_cost(self) -> None:
        usage = TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.02)
        cost = create_task_cost("t1", AgentType.OPENCODE, usage)
        assert cost.reported_cost_usd == pytest.approx(0.02)
        assert cost.estimated_cost_usd == 0.0  # unpriced harness estimate

    def test_create_task_cost_no_reported_cost(self) -> None:
        usage = TokenUsage(input_tokens=10, output_tokens=5)
        cost = create_task_cost("t1", AgentType.CLAUDE_CODE, usage)
        assert cost.reported_cost_usd is None

    def test_parse_and_create_cost_only_log_still_creates_row(
        self, temp_dir: Path
    ) -> None:
        """Zero tokens + reported cost is still a row (relaxed gate)."""
        log_file = temp_dir / "t.log"
        log_file.write_text(
            '{"type": "step_finish", "part": {"cost": 0.02}}\n',
            encoding="utf-8",
        )
        cost = parse_and_create_cost("t1", AgentType.OPENCODE, log_file)
        assert cost is not None
        assert cost.reported_cost_usd == pytest.approx(0.02)
        assert cost.input_tokens == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_database.py -k "reported_cost or migration_4" -q && uv run pytest tests/test_cost_tracker.py -k "reported" -q`
Expected: FAIL — TaskCost has no field `reported_cost_usd` / column missing.

- [ ] **Step 3: Implement**

`maestro/models.py`, in `TaskCost` after `estimated_cost_usd`:

```python
    reported_cost_usd: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Agent-reported cost in USD (e.g. opencode part.cost); "
            "None when the agent did not report one"
        ),
    )
```

`maestro/database.py`:

1. CREATE TABLE DDL — add after the `estimated_cost_usd` line:

```sql
    reported_cost_usd REAL,
```

2. `ordered` migrations list — append (never reorder):

```python
            (
                4,
                "cost_from_log_reported_cost",
                self._migrate_task_costs_reported_cost,
            ),
```

3. New method after `_migrate_rename_zadachi_to_workstreams`:

```python
    async def _migrate_task_costs_reported_cost(self) -> None:
        """cost-from-log: add `reported_cost_usd` to an older `task_costs`.

        NULL for all pre-existing rows — consumers COALESCE to the estimate.
        Idempotent via PRAGMA table_info (same shape as the R-02 migration).
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute("PRAGMA table_info(task_costs)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "reported_cost_usd" not in columns:
            await self._connection.execute(
                "ALTER TABLE task_costs ADD COLUMN reported_cost_usd REAL"
            )
```

4. `save_task_cost` INSERT — column list gains `reported_cost_usd` (7→8 placeholders), values tuple gains `cost.reported_cost_usd` after `cost.estimated_cost_usd`.

5. `_row_to_task_cost` — add `reported_cost_usd=row["reported_cost_usd"],` after the `estimated_cost_usd` line.

`maestro/cost_tracker.py`:

6. `create_task_cost` — add `reported_cost_usd=usage.cost_usd,` to the `TaskCost(...)` call.

7. `parse_and_create_cost` gate:

```python
    usage = parse_log(log_content, agent_type)
    if (
        usage.input_tokens == 0
        and usage.output_tokens == 0
        and usage.cost_usd is None
    ):
        return None
```

- [ ] **Step 4: Run tests, full suite**

Run: `uv run pytest tests/test_database.py tests/test_cost_tracker.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/models.py maestro/database.py maestro/cost_tracker.py tests/test_database.py tests/test_cost_tracker.py
git commit -m "feat(cost): TaskCost.reported_cost_usd + migration #4

Agent-reported cost and PRICING estimate live in separate columns so the
source of every number stays inspectable. Old rows are NULL — consumers
COALESCE to the estimate. Gate relaxed: a cost-only row is still a row."
```

---

### Task 3: Effective-harness dispatch in `Scheduler._record_cost`

**Files:**
- Modify: `maestro/scheduler.py` (`_record_cost`, ~line 307)
- Test: `tests/test_scheduler_cost_recording.py`

**Interfaces:**
- Consumes: `parse_and_create_cost` (Task 2 shape), `harness_of_agent_id` (already imported in scheduler.py — verify, extend the models import if not).
- Produces: TaskCost rows keyed by EFFECTIVE agent type: routed `opencode@glm-5.1` → row `agent_type=OPENCODE` parsed with the opencode parser. Task 4's per-row `effective_cost` relies on this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler_cost_recording.py`:

```python
OPENCODE_LOG = (
    '{"type": "step_finish", "part": {"cost": 0.005, "tokens": '
    '{"input": 100, "output": 20, "reasoning": 0}}}\n'
)


def _make_scheduler(db, tmp_path) -> Scheduler:
    return Scheduler(
        db=db,
        dag=DAG([]),
        spawners={},
        config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
    )


@pytest.mark.anyio
async def test_record_cost_routed_task_uses_effective_harness(tmp_path) -> None:
    """agent_type=auto routed to opencode@glm-5.1: the log is opencode JSONL
    and must be parsed by the opencode parser; the TaskCost row records the
    EFFECTIVE harness (who actually ran), not the declared sentinel."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.AUTO,
            routed_agent_type="opencode@glm-5.1",
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
        await db.create_task(task)
        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        log_file.write_text(OPENCODE_LOG, encoding="utf-8")

        scheduler = _make_scheduler(db, tmp_path)
        running = RunningTask(
            task=task,
            process=MagicMock(),
            started_at=now,
            log_file=log_file,
        )
        await scheduler._record_cost(running)

        rows = await db.get_task_costs("t1")
        assert len(rows) == 1
        assert rows[0].agent_type is AgentType.OPENCODE
        assert rows[0].input_tokens == 100
        assert rows[0].output_tokens == 20
        assert rows[0].reported_cost_usd == pytest.approx(0.005)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_record_cost_declared_override_uses_routed_harness(tmp_path) -> None:
    """Declared claude_code overridden by the arbiter to opencode: the log is
    opencode JSONL — the claude parser would find nothing; the routed harness
    must win the dispatch."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.CLAUDE_CODE,
            routed_agent_type="opencode@glm-5.1",
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
        await db.create_task(task)
        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        log_file.write_text(OPENCODE_LOG, encoding="utf-8")

        scheduler = _make_scheduler(db, tmp_path)
        running = RunningTask(
            task=task, process=MagicMock(), started_at=now, log_file=log_file
        )
        await scheduler._record_cost(running)

        rows = await db.get_task_costs("t1")
        assert len(rows) == 1
        assert rows[0].agent_type is AgentType.OPENCODE
        assert rows[0].reported_cost_usd == pytest.approx(0.005)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_record_cost_non_enum_routed_harness_falls_back(tmp_path) -> None:
    """A D2 custom harness (not an AgentType member) falls back to declared
    dispatch: the declared claude parser finds nothing in opencode JSONL →
    no row, exactly today's behavior."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.CLAUDE_CODE,
            routed_agent_type="fakeharness@x",
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
        await db.create_task(task)
        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        log_file.write_text(OPENCODE_LOG, encoding="utf-8")

        scheduler = _make_scheduler(db, tmp_path)
        running = RunningTask(
            task=task, process=MagicMock(), started_at=now, log_file=log_file
        )
        await scheduler._record_cost(running)

        assert await db.get_task_costs("t1") == []
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify the first two fail**

Run: `uv run pytest tests/test_scheduler_cost_recording.py -q`
Expected: routed tests FAIL (AUTO/claude dispatch finds no usage → no row, or row carries wrong agent_type); the fallback test passes (current behavior).

- [ ] **Step 3: Implement**

In `maestro/scheduler.py`, `_record_cost`, replace the head of the `try` block. First confirm `harness_of_agent_id` is already imported from `maestro.models` (it is used at line ~834); if not, extend that import.

```python
        task = running_task.task
        attempt = task.retry_count + 1
        # Dispatch the parser by the EFFECTIVE harness: a routed task's log
        # was written by the routed agent, not the declared one (agent_type:
        # auto routed to opencode writes opencode JSONL). A routed harness
        # outside AgentType (D2 custom spawner) falls back to the declared
        # type — no parser exists for it anyway, so behavior is unchanged.
        harness = (
            harness_of_agent_id(task.routed_agent_type)
            if task.routed_agent_type
            else task.agent_type.value
        )
        try:
            effective_agent = AgentType(harness)
        except ValueError:
            effective_agent = task.agent_type
        try:
            cost = parse_and_create_cost(
                task_id=task.id,
                agent_type=effective_agent,
                log_file=running_task.log_file,
                attempt=attempt,
            )
```

(the rest of the method — exception handling and `save_task_cost` — is unchanged; keep the existing `except Exception` block intact).

- [ ] **Step 4: Run tests, full suite**

Run: `uv run pytest tests/test_scheduler_cost_recording.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/scheduler.py tests/test_scheduler_cost_recording.py
git commit -m "feat(scheduler): dispatch cost parser by effective harness

Closes the routed-path telemetry gap: agent_type=auto routed to opencode
now reaches parse_opencode_log, and the TaskCost row records who actually
ran. Non-enum (D2 custom) harnesses keep the declared-type fallback."
```

---

### Task 4: `effective_cost` + outcome semantics in `_build_outcome`

**Files:**
- Modify: `maestro/cost_tracker.py` (new `effective_cost` next to `has_pricing`)
- Modify: `maestro/scheduler.py` (`_build_outcome` cost block ~line 370; import swap)
- Test: `tests/test_cost_tracker.py` (`effective_cost` unit), `tests/test_scheduler_cost_recording.py` (outcome)

**Interfaces:**
- Consumes: `TaskCost.reported_cost_usd` (Task 2), `has_pricing` (existing).
- Produces: `effective_cost(cost: TaskCost) -> float | None` in `maestro.cost_tracker` — reported wins, then priced estimate, else None. `_build_outcome` sums it when all rows are known.

- [ ] **Step 1: Write the failing tests**

`tests/test_cost_tracker.py`, new class after `TestHasPricing`:

```python
class TestEffectiveCost:
    """effective_cost: reported wins; estimate only for priced harnesses."""

    def test_reported_wins_over_estimate(self) -> None:
        cost = TaskCost(
            task_id="t",
            agent_type=AgentType.CLAUDE_CODE,
            estimated_cost_usd=0.5,
            reported_cost_usd=0.02,
        )
        assert effective_cost(cost) == pytest.approx(0.02)

    def test_priced_harness_falls_back_to_estimate(self) -> None:
        cost = TaskCost(
            task_id="t",
            agent_type=AgentType.ANNOUNCE,
            estimated_cost_usd=0.0,
        )
        assert effective_cost(cost) == 0.0  # honest zero, not None

    def test_unpriced_unreported_is_unknown(self) -> None:
        cost = TaskCost(task_id="t", agent_type=AgentType.OPENCODE)
        assert effective_cost(cost) is None

    def test_unpriced_reported_is_known(self) -> None:
        cost = TaskCost(
            task_id="t",
            agent_type=AgentType.OPENCODE,
            reported_cost_usd=0.02,
        )
        assert effective_cost(cost) == pytest.approx(0.02)
```

(add `TaskCost` and `effective_cost` to the test file's imports).

`tests/test_scheduler_cost_recording.py`:

```python
@pytest.mark.anyio
async def test_build_outcome_reports_reported_cost(tmp_path) -> None:
    """opencode row WITH agent-reported cost → real dollars to the arbiter."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.OPENCODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)
        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=100,
                output_tokens=20,
                estimated_cost_usd=0.0,
                reported_cost_usd=0.0206,
                attempt=1,
            )
        )
        scheduler = _make_scheduler(db, tmp_path)
        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.cost_usd == pytest.approx(0.0206)
        assert outcome.tokens_used == 120
    finally:
        await db.close()


@pytest.mark.anyio
async def test_build_outcome_mixed_known_unknown_rows_is_none(tmp_path) -> None:
    """Two rows on ONE attempt, one unknown → whole outcome cost is None.
    (Defensive guard — _build_outcome's matching set spans a single attempt;
    closes the deferred minor from PR #42's final review.)"""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.OPENCODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)
        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=10,
                output_tokens=5,
                estimated_cost_usd=0.001,
                attempt=1,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=100,
                output_tokens=20,
                estimated_cost_usd=0.0,
                attempt=1,
            )
        )
        scheduler = _make_scheduler(db, tmp_path)
        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.cost_usd is None
        assert outcome.tokens_used == 135
    finally:
        await db.close()
```

The two existing tests (`test_build_outcome_unpriced_harness_reports_cost_none`, `test_build_outcome_announce_zero_cost_stays_zero`) are the unreported-None and honest-zero regressions — they must stay green unmodified.

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_cost_tracker.py::TestEffectiveCost tests/test_scheduler_cost_recording.py -q`
Expected: `effective_cost` ImportError; `test_build_outcome_reports_reported_cost` fails (current guard sees an unpriced row → None).

- [ ] **Step 3: Implement**

`maestro/cost_tracker.py`, directly after `has_pricing`:

```python
def effective_cost(cost: TaskCost) -> float | None:
    """The best-known cost of one TaskCost row, or None if unknown.

    Agent-reported cost wins; the PRICING estimate is trusted only for
    priced harnesses (announce's 0.0 is an honest zero); an unpriced
    harness with no report is UNKNOWN — callers reporting to the arbiter
    must propagate None, never 0.0.
    """
    if cost.reported_cost_usd is not None:
        return cost.reported_cost_usd
    if has_pricing(cost.agent_type):
        return cost.estimated_cost_usd
    return None
```

`maestro/scheduler.py` — swap the import (`has_pricing` → `effective_cost`; keep `has_pricing` only if still referenced — after this change it is not) and replace the guard in `_build_outcome`:

```python
        matching = [r for r in rows if r.attempt == attempt]
        if matching:
            tokens_used = sum(r.input_tokens + r.output_tokens for r in matching)
            per_row = [effective_cost(r) for r in matching]
            if all(c is not None for c in per_row):
                cost_usd = sum(c for c in per_row if c is not None)
            # else: at least one row's cost is UNKNOWN (unpriced harness
            # with no agent-reported cost) — cost_usd stays None; the
            # arbiter client omits None, so cost-aware routing reads
            # "unknown" instead of "free". Matching spans ONE attempt, so
            # this never zeroes out a different attempt's known cost.
```

- [ ] **Step 4: Run tests, full suite**

Run: `uv run pytest tests/test_cost_tracker.py tests/test_scheduler_cost_recording.py -q && uv run pytest -q`
Expected: PASS, including the two PR-42 regression tests untouched.

- [ ] **Step 5: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/cost_tracker.py maestro/scheduler.py tests/test_cost_tracker.py tests/test_scheduler_cost_recording.py
git commit -m "feat(scheduler): outcome cost = sum of effective per-row costs

effective_cost: reported wins, priced estimate second, else unknown.
Routed opencode with part.cost now reports real dollars to the arbiter;
unreported stays honestly None; announce keeps its honest zero."
```

---

### Task 5: Summaries + benchmark responder prefer reported cost

**Files:**
- Modify: `maestro/database.py` (`get_cost_summary` SQL ~line 1614)
- Modify: `maestro/cost_tracker.py` (`build_summary` ~line 326)
- Modify: `maestro/benchmark/spawner_responder.py` (~line 105-108)
- Test: `tests/test_cost_tracker.py` (build_summary), `tests/test_database.py` (get_cost_summary), `tests/test_spawner_responder.py`

**Interfaces:**
- Consumes: `TaskCost.reported_cost_usd` (Task 2), `TokenUsage.cost_usd` (Task 1).
- Produces: summaries COALESCE reported→estimated (non-nullable floats, unreported-unpriced stays 0.0 — spec §5); responder prefers `usage.cost_usd`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cost_tracker.py`, in `TestBuildSummary`:

```python
    def test_reported_cost_preferred_in_summary(self) -> None:
        """COALESCE semantics: reported wins per row, estimate is fallback."""
        costs = [
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=100,
                output_tokens=20,
                estimated_cost_usd=0.0,
                reported_cost_usd=0.02,
            ),
            TaskCost(
                task_id="t2",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=10,
                output_tokens=5,
                estimated_cost_usd=0.001,
            ),
        ]
        summary = build_summary(costs)
        assert summary.total_cost_usd == pytest.approx(0.021)
        assert summary.costs_by_task["t1"] == pytest.approx(0.02)
        assert summary.costs_by_task["t2"] == pytest.approx(0.001)
```

`tests/test_database.py` (near the existing cost-summary test):

```python
@pytest.mark.anyio
async def test_get_cost_summary_coalesces_reported_cost(tmp_path) -> None:
    from maestro.models import Task, TaskStatus

    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.OPENCODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)
        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=100,
                output_tokens=20,
                estimated_cost_usd=0.0,
                reported_cost_usd=0.02,
                attempt=1,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=10,
                output_tokens=5,
                estimated_cost_usd=0.001,
                attempt=2,
            )
        )
        summary = await db.get_cost_summary()
        assert summary["total_cost_usd"] == pytest.approx(0.021)
    finally:
        await db.close()
```

`tests/test_spawner_responder.py` — follow the file's existing test style (it stubs a spawner whose log the responder parses); add a case where the log is opencode-format JSONL for an opencode agent_type and assert the response's `cost_usd` equals the reported value, not the PRICING-computed one:

```python
@pytest.mark.anyio
async def test_reported_cost_preferred_over_pricing(tmp_path, monkeypatch):
    """When the parsed usage carries agent-reported cost, the responder
    forwards it instead of the PRICING estimate (0.0 for opencode)."""
    # Arrange a fake spawner writing opencode JSONL, mirroring the module's
    # existing happy-path test setup, with agent_type "opencode" and log:
    # {"type": "step_finish", "part": {"cost": 0.02, "tokens":
    #  {"input": 100, "output": 20, "reasoning": 0}}}
    # Assert: response.cost_usd == pytest.approx(0.02)
    # Assert: response.tokens_used == 120
```

(The implementer adapts the arrange-block from the existing happy-path test in that file — the file's fake-spawner scaffolding is task-specific and must be reused, not reinvented. The two assertions above are the contract.)

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_cost_tracker.py::TestBuildSummary tests/test_database.py -k cost_summary tests/test_spawner_responder.py -q`
Expected: new tests FAIL (summaries sum only estimates; responder computes PRICING cost = 0 → None).

- [ ] **Step 3: Implement**

`maestro/database.py`, `get_cost_summary` SQL — replace the cost line:

```sql
                COALESCE(SUM(COALESCE(reported_cost_usd, estimated_cost_usd)), 0.0) as total_cost_usd,
```

`maestro/cost_tracker.py`, `build_summary` loop — replace both `estimated_cost_usd` reads:

```python
    for cost in costs:
        summary.total_input_tokens += cost.input_tokens
        summary.total_output_tokens += cost.output_tokens
        # COALESCE semantics (same as get_cost_summary's SQL): reported
        # wins; the estimate is the fallback. Summaries stay non-nullable
        # floats — the None-vs-0 distinction lives only at the
        # arbiter-outcome boundary (effective_cost).
        row_cost = (
            cost.reported_cost_usd
            if cost.reported_cost_usd is not None
            else cost.estimated_cost_usd
        )
        summary.total_cost_usd += row_cost
        task_ids.add(cost.task_id)

        if cost.task_id not in summary.costs_by_task:
            summary.costs_by_task[cost.task_id] = 0.0
        summary.costs_by_task[cost.task_id] += row_cost
```

`maestro/benchmark/spawner_responder.py` — replace the cost line:

```python
        usage = parse_log(log_content, agent_enum)
        total_tokens = usage.input_tokens + usage.output_tokens
        # Agent-reported cost wins over the PRICING estimate; the trailing
        # `cost or None` guards below keep 0.0 out of the wire format.
        cost = (
            usage.cost_usd
            if usage.cost_usd is not None
            else calculate_cost(usage, agent_enum)
        )
```

- [ ] **Step 4: Run tests, full suite**

Run: `uv run pytest tests/test_cost_tracker.py tests/test_database.py tests/test_spawner_responder.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/database.py maestro/cost_tracker.py maestro/benchmark/spawner_responder.py tests/test_cost_tracker.py tests/test_database.py tests/test_spawner_responder.py
git commit -m "feat(cost): summaries and benchmark responder prefer reported cost

REST/dashboard/CLI summaries COALESCE reported->estimated; opencode
stops showing \$0.00 where a real cost was reported. Responder forwards
usage.cost_usd when present."
```

---

### Task 6: Docs, follow-up ticks, final gates

**Files:**
- Modify: `TODO.md` (opencode follow-ups section)
- Modify: `CLAUDE.md` (spawners bullet)

**Interfaces:**
- Consumes: everything above merged.
- Produces: honest docs; ticked follow-ups #1 and #3.

- [ ] **Step 1: Tick TODO.md follow-ups**

In `## opencode follow-ups (ADR-ECO-003c)`: mark the cost-from-log item and the routed-path token telemetry item `[x]`, each with a trailing note `(closed by feat/cost-from-log, <merge-commit or branch-head SHA>)`. Leave the SSOT catalog item `[ ]`. Do not delete the constraint text — history stays readable.

- [ ] **Step 2: Update CLAUDE.md spawners bullet**

Replace the sentence "Its cost is reported to the arbiter as unknown (`cost_usd=None`, not 0.0) until cost-from-log lands" with:

```
Its cost comes from opencode's own per-step `part.cost` (persisted as `TaskCost.reported_cost_usd`); when the log reports none, the arbiter sees unknown (`cost_usd=None`), never 0.0
```

- [ ] **Step 3: Final gates + smoke**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
git status --short
```

Expected: suite green (~1360 + ~20 new), pyrefly clean, ruff clean, only intended files.

Smoke (config-to-summary path):

```bash
uv run python -c "
from maestro.cost_tracker import parse_opencode_log, effective_cost, create_task_cost
from maestro.models import AgentType
from pathlib import Path
usage = parse_opencode_log(Path('tests/fixtures/opencode_run.jsonl').read_text())
cost = create_task_cost('smoke', AgentType.OPENCODE, usage)
print('reported:', cost.reported_cost_usd, '| effective:', effective_cost(cost))
"
```

Expected: `reported: 0.02064656 | effective: 0.02064656`.

- [ ] **Step 4: Commit docs**

```bash
git add TODO.md CLAUDE.md
git commit -m "docs: cost-from-log shipped — tick opencode follow-ups #1 and #3"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final whole-branch review)

```bash
git push -u origin feat/cost-from-log
gh pr create --title "feat(cost): opencode cost-from-log + effective-harness dispatch" --body "$(cat <<'EOF'
## Summary
- parse_opencode_log extracts per-step part.cost into TokenUsage.cost_usd (per-step semantics fixture-proven; bool guard; None when unreported)
- TaskCost.reported_cost_usd (migration #4): agent truth and PRICING estimate stay separate, inspectable columns
- Scheduler._record_cost dispatches the parser by EFFECTIVE harness — closes the routed-path telemetry gap (agent_type: auto → opencode now parsed)
- Scheduler._build_outcome reports sum of effective per-row costs: reported > priced estimate > unknown(None); arbiter gets real opencode dollars
- Summaries (REST/dashboard/CLI) and benchmark responder COALESCE reported→estimated — opencode stops showing $0.00
- Cache constraint honored by construction: cost is never computed from tokens for opencode

Known limitation (in spec): TaskCost.agent_type changes meaning declared→effective at the migration boundary, no backfill; audited readers unaffected.

Spec: docs/superpowers/specs/2026-07-05-cost-from-log-design.md

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] Real-fixture cost literal (0.02064656) locks per-step summing
- [ ] Migration #4 on a pre-#4 database + round-trip incl. None
- [ ] Routed auto→opencode records an OPENCODE row with reported cost
- [ ] Outcome: reported → real sum; unreported → None; mixed → None; announce → 0.0

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: §1→Task 1; §2→Task 2; §3→Task 3; §4→Task 4 (incl. attempt-scoping comment and PR-42 regressions); §5+§6→Task 5; §7 tests distributed per task; §8→Task 6; known-limitation → PR body + spec.
- Type consistency: `effective_cost(cost: TaskCost) -> float | None` (Task 4, consumed nowhere else by name); `TokenUsage.cost_usd: float | None` (Tasks 1→2→5); `reported_cost_usd` field name identical across models/DB/tests.
- The one intentionally non-literal test block (responder, Task 5 Step 1) delegates the arrange-scaffolding to the file's existing fake-spawner pattern with the contract assertions spelled out — reinventing that scaffolding in the plan would guarantee drift.
