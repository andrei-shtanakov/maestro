"""R-NN: scheduler wires `cost_tracker` so TaskOutcome carries real values.

Verifies the end-to-end path from agent log (JSON with usage) → `task_costs`
row → `TaskOutcome.tokens_used`/`cost_usd` populated via `_build_outcome`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig


def _write_claude_log(log_file, input_tokens: int, output_tokens: int) -> None:
    """Emit a Claude Code-style JSON log with usage the tracker can parse."""
    log_file.write_text(
        '{"result": "ok", "usage": {"input_tokens": '
        f"{input_tokens}, "
        f'"output_tokens": {output_tokens}'
        "}}",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_success_records_cost_from_agent_log(tmp_path) -> None:
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
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
        await db.create_task(task)

        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        _write_claude_log(log_file, input_tokens=100, output_tokens=50)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        proc = MagicMock()
        proc.poll.return_value = 0
        running = RunningTask(
            task=task,
            handle=proc,
            started_at=task.created_at,
            log_file=log_file,
        )

        await scheduler._handle_task_completion("t1", running, return_code=0)

        costs = await db.get_task_costs("t1")
        assert len(costs) == 1
        row = costs[0]
        assert row.attempt == 1
        assert row.input_tokens == 100
        assert row.output_tokens == 50
        # Pricing: claude_code $3/M input + $15/M output
        # 100 * 3/1e6 + 50 * 15/1e6 = 0.0003 + 0.00075 = 0.00105
        assert row.estimated_cost_usd == pytest.approx(0.00105, rel=1e-6)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_build_outcome_surfaces_recorded_cost(tmp_path) -> None:
    """_build_outcome must read the freshly-recorded cost row back out."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        # Prepopulate cost row as if a prior recording step ran.
        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=200,
                output_tokens=100,
                estimated_cost_usd=0.0021,
                attempt=1,
            )
        )

        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.tokens_used == 300
        assert outcome.cost_usd == pytest.approx(0.0021, rel=1e-6)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_failure_path_records_cost_at_attempt_one(tmp_path) -> None:
    """The failing run is attempt 1 even after retry_count bumps to 1;
    _build_outcome must look up attempt=1 and find the cost."""
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
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
            max_retries=2,
        )
        await db.create_task(task)

        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        _write_claude_log(log_file, input_tokens=10, output_tokens=5)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        proc = MagicMock()
        proc.poll.return_value = 1
        running = RunningTask(
            task=task,
            handle=proc,
            started_at=task.created_at,
            log_file=log_file,
        )

        await scheduler._handle_task_completion("t1", running, return_code=1)

        costs = await db.get_task_costs("t1")
        assert len(costs) == 1
        assert costs[0].attempt == 1  # The attempt that just failed
        # retry_count has been bumped to 1 inside _handle_task_failure; the
        # cost row for the just-finished run must still be keyed at 1.
        refetched = await db.get_task("t1")
        assert refetched.retry_count == 1
    finally:
        await db.close()


@pytest.mark.anyio
async def test_build_outcome_unpriced_harness_reports_cost_none(
    tmp_path,
) -> None:
    """opencode (no PRICING entry): cost 0.0 would read as 'free' to
    cost-aware routing (R-07 'route cheapest sufficient'), so _build_outcome
    must report cost_usd=None (unknown) while still reporting real tokens."""
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

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=250,
                output_tokens=55,
                estimated_cost_usd=0.0,  # unpriced harness records 0.0
                attempt=1,
            )
        )

        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.tokens_used == 305  # tokens are real and reported
        assert outcome.cost_usd is None  # cost is UNKNOWN, not free
    finally:
        await db.close()


@pytest.mark.anyio
async def test_build_outcome_announce_zero_cost_stays_zero(
    tmp_path,
) -> None:
    """announce IS in PRICING at (0.0, 0.0) — an honest zero, not unknown."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.ANNOUNCE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.ANNOUNCE,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                attempt=1,
            )
        )

        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.cost_usd == 0.0
    finally:
        await db.close()


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
            handle=MagicMock(),
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
            task=task, handle=MagicMock(), started_at=now, log_file=log_file
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
            task=task, handle=MagicMock(), started_at=now, log_file=log_file
        )
        await scheduler._record_cost(running)

        assert await db.get_task_costs("t1") == []
    finally:
        await db.close()


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
