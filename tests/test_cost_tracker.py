"""Tests for cost tracking: log parsing, cost calculation, and summary.

This module contains unit tests for the cost_tracker module covering
log parsing for different agent types, cost calculation, and summary
reporting. Also includes database integration tests for task_costs table.
"""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.cost_tracker import (
    CostSummary,
    TokenUsage,
    build_summary,
    calculate_cost,
    create_task_cost,
    effective_cost,
    format_summary,
    has_pricing,
    parse_and_create_cost,
    parse_claude_code_log,
    parse_log,
    parse_opencode_log,
)
from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskCost, TaskStatus


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    """Provide a connected and initialized database."""
    database = await create_database(temp_db_path)
    yield database
    await database.close()


@pytest.fixture
def sample_task() -> Task:
    """Provide a sample task for cost testing."""
    return Task(
        id="task-001",
        title="Test Task",
        prompt="Test prompt",
        workdir="/tmp/test",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.PENDING,
    )


# =============================================================================
# Log Parsing Tests
# =============================================================================


class TestClaudeCodeLogParsing:
    """Tests for Claude Code JSON log parsing."""

    def test_parse_empty_log(self) -> None:
        """Empty log returns zero tokens."""
        usage = parse_claude_code_log("")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_whitespace_log(self) -> None:
        """Whitespace-only log returns zero tokens."""
        usage = parse_claude_code_log("   \n\n  ")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_top_level_tokens(self) -> None:
        """Parse tokens from top-level JSON fields."""
        log = json.dumps(
            {
                "result": "some output",
                "input_tokens": 1500,
                "output_tokens": 500,
            }
        )
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 1500
        assert usage.output_tokens == 500

    def test_parse_nested_usage(self) -> None:
        """Parse tokens from nested usage object."""
        log = json.dumps(
            {
                "result": "some output",
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 800,
                },
            }
        )
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 2000
        assert usage.output_tokens == 800

    def test_parse_partial_usage(self) -> None:
        """Parse usage with only one token field."""
        log = json.dumps(
            {
                "usage": {
                    "input_tokens": 1000,
                },
            }
        )
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 0

    def test_parse_last_json_line(self) -> None:
        """Parse tokens from last JSON line in multi-line output."""
        lines = [
            "Starting task...",
            "Processing...",
            json.dumps(
                {
                    "input_tokens": 3000,
                    "output_tokens": 1200,
                }
            ),
        ]
        log = "\n".join(lines)
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 3000
        assert usage.output_tokens == 1200

    def test_parse_invalid_json(self) -> None:
        """Invalid JSON returns zero tokens."""
        usage = parse_claude_code_log("not json at all")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_json_without_tokens(self) -> None:
        """JSON without token fields returns zero tokens."""
        log = json.dumps({"result": "completed", "status": "ok"})
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_json_array(self) -> None:
        """JSON array (non-dict) returns zero tokens."""
        log = json.dumps([1, 2, 3])
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_mixed_output_with_json(self) -> None:
        """Mixed text and JSON, last line has usage."""
        lines = [
            "Running claude --print --output-format json",
            "Error on line 5",
            '{"usage": {"input_tokens": 500, "output_tokens": 200}}',
        ]
        log = "\n".join(lines)
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 500
        assert usage.output_tokens == 200

    def test_claude_log_extracts_total_cost_usd(self) -> None:
        """Claude's ``total_cost_usd`` is extracted into cost_usd."""
        content = json.dumps(
            {
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": 0.0123,
            }
        )
        usage = parse_claude_code_log(content)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cost_usd == pytest.approx(0.0123)

    def test_claude_log_cost_usd_key_fallback(self) -> None:
        """Falls back to ``cost_usd`` when ``total_cost_usd`` is absent."""
        content = json.dumps({"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5})
        usage = parse_claude_code_log(content)
        assert usage.cost_usd == pytest.approx(0.5)

    def test_claude_log_rejects_bad_cost_values(self) -> None:
        """bool/NaN/Infinity/negative costs are rejected, tokens still parsed."""
        for bad in ("true", "NaN", "Infinity", "-1.0"):
            content = (
                '{"usage": {"input_tokens": 10, "output_tokens": 5}, '
                f'"total_cost_usd": {bad}}}'
            )
            usage = parse_claude_code_log(content)
            assert usage.cost_usd is None, f"cost {bad} must be rejected"
            assert usage.input_tokens == 10  # tokens still parsed

    def test_claude_log_cost_only_survives_zero_tokens(self) -> None:
        """A result with a cost but no token fields must not be dropped."""
        content = json.dumps({"total_cost_usd": 0.02})
        usage = parse_claude_code_log(content)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cost_usd == pytest.approx(0.02)


class TestOpencodeLogParsing:
    """Tests for opencode `run --format json` JSONL parsing."""

    FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run.jsonl"

    def test_parse_real_fixture_sums_step_finish(self) -> None:
        """Real captured run: per-step tokens summed across step_finish events.

        Expected literals were computed with jq over the fixture (independent
        of this parser), so this is not a tautology. Guards the per-step (not
        cumulative) aggregation verdict from the fixture-capture task.
        """
        usage = parse_opencode_log(self.FIXTURE.read_text(encoding="utf-8"))
        assert usage.input_tokens == 11940
        assert usage.output_tokens == 201

    def test_reasoning_counted_into_output(self) -> None:
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5, "reasoning": 7}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 12

    def test_multiple_steps_summed(self) -> None:
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 100, "output": 20, "reasoning": 0}}}\n'
            '{"type": "tool_use", "part": {"name": "read"}}\n'
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 150, "output": 30, "reasoning": 5}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 250
        assert usage.output_tokens == 55

    def test_malformed_lines_skipped(self) -> None:
        log = (
            "stderr noise: model warming up\n"
            "{not json at all\n"
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5

    def test_missing_tokens_fields_default_zero(self) -> None:
        log = '{"type": "step_finish", "part": {"tokens": {"output": 3}}}\n'
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 3

    def test_explicit_null_token_fields_default_zero(self) -> None:
        """opencode may emit explicit null for unreported sub-metrics."""
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": null, "output": 3, "reasoning": null}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 3

    def test_non_step_finish_events_ignored(self) -> None:
        log = (
            '{"type": "step_start", "part": {}}\n'
            '{"type": "error", "part": {"tokens": {"input": 999, "output": 999}}}\n'
        )
        assert parse_opencode_log(log) == TokenUsage()

    def test_empty_log(self) -> None:
        assert parse_opencode_log("") == TokenUsage()

    def test_nonempty_log_without_step_finish_logs_drift_canary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Format-drift canary: non-empty log, zero step_finish → debug log,
        so a silent event rename in opencode doesn't quietly zero tracking."""
        import logging

        log = '{"type": "step_done", "part": {"tokens": {"input": 5}}}\n'
        with caplog.at_level(logging.DEBUG, logger="maestro.cost_tracker"):
            usage = parse_opencode_log(log)
        assert usage == TokenUsage()
        assert "no step_finish" in caplog.text

    def test_step_finish_without_part_tokens_skipped(self) -> None:
        log = (
            '{"type": "step_finish"}\n'
            '{"type": "step_finish", "part": {}}\n'
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 1, "output": 2}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 1
        assert usage.output_tokens == 2

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
            '{"type": "step_finish", "part": {"tokens": {"input": 10, "output": 5}}}\n'
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

    def test_negative_cost_ignored_tokens_kept(self) -> None:
        """A negative part.cost is rejected (a negative sum would fail
        TaskCost's ge=0.0 and silently drop the whole row); tokens are kept."""
        log = (
            '{"type": "step_finish", "part": {"cost": -0.5, "tokens": '
            '{"input": 10, "output": 5}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.cost_usd is None
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5

    def test_negative_cost_skipped_positive_summed(self) -> None:
        """Negative per-step cost is skipped; only non-negative steps sum."""
        log = (
            '{"type": "step_finish", "part": {"cost": -1.0, "tokens": '
            '{"input": 1, "output": 1}}}\n'
            '{"type": "step_finish", "part": {"cost": 0.02, "tokens": '
            '{"input": 2, "output": 2}}}\n'
        )
        assert parse_opencode_log(log).cost_usd == pytest.approx(0.02)

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

    def test_infinity_cost_ignored(self) -> None:
        """Infinity must not leak in and poison downstream summaries."""
        log = (
            '{"type": "step_finish", "part": {"cost": Infinity, "tokens": '
            '{"input": 1, "output": 1}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.cost_usd is None
        assert usage.input_tokens == 1

    def test_nan_cost_ignored(self) -> None:
        """NaN must not fail the downstream ge=0.0 check and drop the row."""
        log = (
            '{"type": "step_finish", "part": {"cost": NaN, "tokens": '
            '{"input": 1, "output": 1}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.cost_usd is None
        assert usage.input_tokens == 1


class TestHasPricing:
    """PRICING membership = "this harness has a priced rate card"."""

    def test_opencode_is_unpriced(self) -> None:
        assert has_pricing(AgentType.OPENCODE) is False

    def test_announce_zero_is_an_honest_price(self) -> None:
        assert has_pricing(AgentType.ANNOUNCE) is True

    def test_priced_harnesses(self) -> None:
        assert has_pricing(AgentType.CLAUDE_CODE) is True
        assert has_pricing(AgentType.CODEX) is True
        assert has_pricing(AgentType.AIDER) is True


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


class TestParseLog:
    """Tests for the generic parse_log dispatcher."""

    def test_parse_log_claude_code(self) -> None:
        """Dispatch to Claude Code parser."""
        log = json.dumps({"input_tokens": 100, "output_tokens": 50})
        usage = parse_log(log, AgentType.CLAUDE_CODE)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50

    def test_parse_log_codex(self) -> None:
        """Dispatch to Codex parser."""
        log = json.dumps({"input_tokens": 200, "output_tokens": 75})
        usage = parse_log(log, AgentType.CODEX)
        assert usage.input_tokens == 200
        assert usage.output_tokens == 75

    def test_parse_log_aider(self) -> None:
        """Dispatch to Aider parser."""
        log = json.dumps({"input_tokens": 300, "output_tokens": 100})
        usage = parse_log(log, AgentType.AIDER)
        assert usage.input_tokens == 300
        assert usage.output_tokens == 100

    def test_parse_log_announce(self) -> None:
        """Announce agent type returns zero tokens."""
        usage = parse_log("anything", AgentType.ANNOUNCE)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_log_opencode(self) -> None:
        """parse_log dispatches OPENCODE to the JSONL parser."""
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5, "reasoning": 1}}}\n'
        )
        usage = parse_log(log, AgentType.OPENCODE)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 6


# =============================================================================
# Cost Calculation Tests
# =============================================================================


class TestCostCalculation:
    """Tests for cost calculation from token usage."""

    def test_calculate_cost_claude_code(self) -> None:
        """Calculate cost for Claude Code usage."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
        cost = calculate_cost(usage, AgentType.CLAUDE_CODE)
        # input: 1M * $3/1M = $3.00, output: 100K * $15/1M = $1.50
        assert abs(cost - 4.50) < 0.001

    def test_calculate_cost_codex(self) -> None:
        """Calculate cost for Codex usage."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
        cost = calculate_cost(usage, AgentType.CODEX)
        # input: 1M * $2.5/1M = $2.50, output: 100K * $10/1M = $1.00
        assert abs(cost - 3.50) < 0.001

    def test_calculate_cost_zero_tokens(self) -> None:
        """Zero tokens yield zero cost."""
        usage = TokenUsage(input_tokens=0, output_tokens=0)
        cost = calculate_cost(usage, AgentType.CLAUDE_CODE)
        assert cost == 0.0

    def test_calculate_cost_announce(self) -> None:
        """Announce agent type always has zero cost."""
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = calculate_cost(usage, AgentType.ANNOUNCE)
        assert cost == 0.0

    def test_calculate_cost_small_usage(self) -> None:
        """Calculate cost for small token counts."""
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = calculate_cost(usage, AgentType.CLAUDE_CODE)
        # input: 1000 * $3/1M = $0.003, output: 500 * $15/1M = $0.0075
        expected = 0.003 + 0.0075
        assert abs(cost - expected) < 0.0001

    def test_calculate_cost_opencode_unpriced_is_zero(self) -> None:
        """No PRICING entry → calculate_cost falls back to 0.0 (TaskCost rows
        keep recording 0.0); outcome reporting turns that into None upstream."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert calculate_cost(usage, AgentType.OPENCODE) == 0.0


class TestCreateTaskCost:
    """Tests for creating TaskCost records."""

    def test_create_task_cost(self) -> None:
        """Create TaskCost from token usage."""
        usage = TokenUsage(input_tokens=5000, output_tokens=2000)
        cost = create_task_cost("task-001", AgentType.CLAUDE_CODE, usage, attempt=1)
        assert cost.task_id == "task-001"
        assert cost.agent_type == AgentType.CLAUDE_CODE
        assert cost.input_tokens == 5000
        assert cost.output_tokens == 2000
        assert cost.estimated_cost_usd > 0.0
        assert cost.attempt == 1

    def test_create_task_cost_retry(self) -> None:
        """Create TaskCost with retry attempt number."""
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = create_task_cost("task-002", AgentType.CODEX, usage, attempt=3)
        assert cost.attempt == 3

    def test_create_task_cost_carries_reported_cost(self) -> None:
        usage = TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.02)
        cost = create_task_cost("t1", AgentType.OPENCODE, usage)
        assert cost.reported_cost_usd == pytest.approx(0.02)
        assert cost.estimated_cost_usd == 0.0  # unpriced harness estimate

    def test_create_task_cost_no_reported_cost(self) -> None:
        usage = TokenUsage(input_tokens=10, output_tokens=5)
        cost = create_task_cost("t1", AgentType.CLAUDE_CODE, usage)
        assert cost.reported_cost_usd is None


class TestParseAndCreateCost:
    """Tests for the parse_and_create_cost convenience function."""

    def test_parse_and_create_from_file(self, temp_dir: Path) -> None:
        """Parse log file and create TaskCost."""
        log_file = temp_dir / "task-001.log"
        log_data = json.dumps(
            {
                "input_tokens": 5000,
                "output_tokens": 2000,
            }
        )
        log_file.write_text(log_data)

        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is not None
        assert result.input_tokens == 5000
        assert result.output_tokens == 2000
        assert result.estimated_cost_usd > 0.0

    def test_parse_missing_file(self, temp_dir: Path) -> None:
        """Missing log file returns None."""
        log_file = temp_dir / "nonexistent.log"
        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is None

    def test_parse_empty_file(self, temp_dir: Path) -> None:
        """Empty log file returns None."""
        log_file = temp_dir / "empty.log"
        log_file.write_text("")
        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is None

    def test_parse_no_usage_data(self, temp_dir: Path) -> None:
        """Log without usage data returns None."""
        log_file = temp_dir / "no-usage.log"
        log_file.write_text('{"result": "done"}')
        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is None

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

    def test_parse_and_create_cost_from_cost_only_log(self, tmp_path: Path) -> None:
        """Claude cost-only log (no tokens) still produces a TaskCost row."""
        log = tmp_path / "c.log"
        log.write_text(json.dumps({"total_cost_usd": 0.02}))
        tc = parse_and_create_cost("t1", AgentType.CLAUDE_CODE, log)
        assert tc is not None
        assert tc.reported_cost_usd == pytest.approx(0.02)


# =============================================================================
# Summary Report Tests
# =============================================================================


class TestBuildSummary:
    """Tests for building cost summary."""

    def test_empty_costs(self) -> None:
        """Empty cost list produces zero summary."""
        summary = build_summary([])
        assert summary.total_input_tokens == 0
        assert summary.total_output_tokens == 0
        assert summary.total_cost_usd == 0.0
        assert summary.task_count == 0
        assert summary.costs_by_task == {}

    def test_single_cost(self) -> None:
        """Summary from a single cost record."""
        cost = TaskCost(
            task_id="task-001",
            agent_type=AgentType.CLAUDE_CODE,
            input_tokens=1000,
            output_tokens=500,
            estimated_cost_usd=0.0105,
            attempt=1,
        )
        summary = build_summary([cost])
        assert summary.total_input_tokens == 1000
        assert summary.total_output_tokens == 500
        assert summary.task_count == 1
        assert "task-001" in summary.costs_by_task

    def test_multiple_tasks(self) -> None:
        """Summary from costs across multiple tasks."""
        costs = [
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
                attempt=1,
            ),
            TaskCost(
                task_id="task-002",
                agent_type=AgentType.CODEX,
                input_tokens=2000,
                output_tokens=800,
                estimated_cost_usd=0.02,
                attempt=1,
            ),
        ]
        summary = build_summary(costs)
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 1300
        assert summary.task_count == 2
        assert len(summary.costs_by_task) == 2

    def test_multiple_attempts_same_task(self) -> None:
        """Summary aggregates costs from retry attempts."""
        costs = [
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
                attempt=1,
            ),
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1200,
                output_tokens=600,
                estimated_cost_usd=0.012,
                attempt=2,
            ),
        ]
        summary = build_summary(costs)
        assert summary.total_input_tokens == 2200
        assert summary.total_output_tokens == 1100
        assert summary.task_count == 1
        assert abs(summary.costs_by_task["task-001"] - 0.022) < 0.001

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


class TestFormatSummary:
    """Tests for formatting cost summary as text."""

    def test_format_empty_summary(self) -> None:
        """Format an empty summary."""
        summary = CostSummary()
        report = format_summary(summary)
        assert "Cost Summary" in report
        assert "Tasks tracked: 0" in report
        assert "$0.0000" in report

    def test_format_with_data(self) -> None:
        """Format a summary with data."""
        summary = CostSummary(
            total_input_tokens=5000,
            total_output_tokens=2000,
            total_cost_usd=0.045,
            task_count=2,
            costs_by_task={
                "task-001": 0.025,
                "task-002": 0.020,
            },
        )
        report = format_summary(summary)
        assert "Tasks tracked: 2" in report
        assert "5,000" in report
        assert "2,000" in report
        assert "$0.0450" in report
        assert "task-001: $0.0250" in report
        assert "task-002: $0.0200" in report


# =============================================================================
# Database Integration Tests
# =============================================================================


class TestTaskCostDatabase:
    """Tests for task_costs database operations."""

    @pytest.mark.anyio
    async def test_save_and_get_task_cost(
        self, db: Database, sample_task: Task
    ) -> None:
        """Save and retrieve a task cost."""
        await db.create_task(sample_task)

        cost = TaskCost(
            task_id="task-001",
            agent_type=AgentType.CLAUDE_CODE,
            input_tokens=5000,
            output_tokens=2000,
            estimated_cost_usd=0.045,
            attempt=1,
        )
        saved = await db.save_task_cost(cost)
        assert saved.id is not None

        costs = await db.get_task_costs("task-001")
        assert len(costs) == 1
        assert costs[0].input_tokens == 5000
        assert costs[0].output_tokens == 2000
        assert costs[0].estimated_cost_usd == 0.045

    @pytest.mark.anyio
    async def test_multiple_attempts(self, db: Database, sample_task: Task) -> None:
        """Save costs for multiple retry attempts."""
        await db.create_task(sample_task)

        for attempt in range(1, 4):
            cost = TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000 * attempt,
                output_tokens=500 * attempt,
                estimated_cost_usd=0.01 * attempt,
                attempt=attempt,
            )
            await db.save_task_cost(cost)

        costs = await db.get_task_costs("task-001")
        assert len(costs) == 3
        assert costs[0].attempt == 1
        assert costs[2].attempt == 3

    @pytest.mark.anyio
    async def test_get_all_costs(self, db: Database, sample_task: Task) -> None:
        """Get all cost records across tasks."""
        await db.create_task(sample_task)

        task2 = Task(
            id="task-002",
            title="Task Two",
            prompt="Second task",
            workdir="/tmp/test",
            agent_type=AgentType.CODEX,
        )
        await db.create_task(task2)

        await db.save_task_cost(
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="task-002",
                agent_type=AgentType.CODEX,
                input_tokens=2000,
                output_tokens=800,
                estimated_cost_usd=0.02,
            )
        )

        all_costs = await db.get_all_costs()
        assert len(all_costs) == 2

    @pytest.mark.anyio
    async def test_get_cost_summary(self, db: Database, sample_task: Task) -> None:
        """Get aggregated cost summary."""
        await db.create_task(sample_task)

        await db.save_task_cost(
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=2000,
                output_tokens=800,
                estimated_cost_usd=0.02,
            )
        )

        summary = await db.get_cost_summary()
        assert summary["total_input_tokens"] == 3000
        assert summary["total_output_tokens"] == 1300
        assert abs(summary["total_cost_usd"] - 0.03) < 0.001
        assert summary["task_count"] == 1

    @pytest.mark.anyio
    async def test_get_cost_summary_empty(self, db: Database) -> None:
        """Empty database returns zero summary."""
        summary = await db.get_cost_summary()
        assert summary["total_input_tokens"] == 0
        assert summary["total_output_tokens"] == 0
        assert summary["total_cost_usd"] == 0.0
        assert summary["task_count"] == 0

    @pytest.mark.anyio
    async def test_get_task_costs_empty(self, db: Database) -> None:
        """No costs for a task returns empty list."""
        costs = await db.get_task_costs("nonexistent-task")
        assert costs == []
