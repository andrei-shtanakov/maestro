import pytest
from pydantic import ValidationError

from maestro.benchmark.arbiter_report import WireTaskResult
from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult


def _domain_task(**kwargs):
    defaults = {
        "task_index": 0,
        "prompt": "p",
        "response": "r",
        "duration_seconds": 1.0,
    }
    defaults.update(kwargs)
    return BenchmarkTaskResult(**defaults)


def test_wire_task_result_excludes_prompt_and_response():
    """Free-form strip: WireTaskResult must not carry prompt/response."""
    wire = WireTaskResult.from_domain(
        _domain_task(prompt="long prompt", response="long resp")
    )
    dumped = wire.model_dump()
    assert "prompt" not in dumped
    assert "response" not in dumped


def test_wire_task_result_maps_domain_fields():
    wire = WireTaskResult.from_domain(
        _domain_task(
            task_index=3,
            duration_seconds=4.2,
            tokens_used=1234,
            task_type="bugfix",
            score=0.9,
            error=None,
        )
    )
    assert wire.task_index == 3
    assert wire.duration_seconds == 4.2
    assert wire.tokens_used == 1234
    assert wire.task_type == "bugfix"
    assert wire.score == 0.9
    assert wire.error_class is None


def test_wire_task_result_error_bucketing():
    """Free-form error message → bounded enum bucket."""
    assert (
        WireTaskResult.from_domain(_domain_task(error="timeout after 30s")).error_class
        == "timeout"
    )
    assert (
        WireTaskResult.from_domain(_domain_task(error="subprocess crashed")).error_class
        == "crash"
    )
    assert (
        WireTaskResult.from_domain(_domain_task(error="2 test failures")).error_class
        == "test_failure"
    )
    assert (
        WireTaskResult.from_domain(_domain_task(error="something else")).error_class
        == "other"
    )
    assert WireTaskResult.from_domain(_domain_task(error=None)).error_class is None


def test_wire_task_result_forbids_extra_fields():
    with pytest.raises(ValidationError):
        WireTaskResult(
            task_index=0,
            task_type=None,
            score=None,
            tokens_used=None,
            duration_seconds=1.0,
            error_class=None,
            surprise="boom",
        )


# ---------------------------------------------------------------------------
# Task 4.2 — ReportBenchmarkPayload, _sample_per_task, _build_wire_payload
# ---------------------------------------------------------------------------

from maestro.benchmark.arbiter_report import (  # noqa: E402
    _build_wire_payload,
)


def _result(run_id: str = "r", per_task: list | None = None, score: float = 0.5):
    return BenchmarkResult(
        run_id=run_id,
        benchmark_id="b",
        agent_id="a",
        score=score,
        per_task=per_task or [],
        duration_seconds=1.0,
    )


def _tasks(n: int) -> list[BenchmarkTaskResult]:
    return [
        BenchmarkTaskResult(
            task_index=i, prompt=f"p{i}", response=f"r{i}", duration_seconds=1.0
        )
        for i in range(n)
    ]


def test_payload_version_pinned_to_1_0_0():
    p = _build_wire_payload(_result(), max_per_task=200)
    assert p.payload_version == "1.0.0"


def test_payload_maps_all_aggregate_fields():
    result = _result(run_id="x", per_task=_tasks(3), score=0.85)
    p = _build_wire_payload(result, max_per_task=200)
    assert p.run_id == "x"
    assert p.benchmark_id == "b"
    assert p.agent_id == "a"
    assert p.score == 0.85
    assert p.per_task_total_count == 3
    assert p.per_task_truncated is False
    assert len(p.per_task) == 3


def test_truncation_under_cap_no_change():
    p = _build_wire_payload(_result(per_task=_tasks(50)), max_per_task=200)
    assert p.per_task_truncated is False
    assert len(p.per_task) == 50
    assert p.per_task_total_count == 50


def test_truncation_at_cap_boundary_not_truncated():
    p = _build_wire_payload(_result(per_task=_tasks(200)), max_per_task=200)
    assert p.per_task_truncated is False
    assert len(p.per_task) == 200


def test_truncation_above_cap_samples():
    p = _build_wire_payload(_result(per_task=_tasks(500)), max_per_task=200)
    assert p.per_task_truncated is True
    assert len(p.per_task) == 200
    assert p.per_task_total_count == 500


def test_truncation_deterministic_same_run_id_same_sample():
    tasks = _tasks(500)
    p1 = _build_wire_payload(_result(run_id="same", per_task=tasks), max_per_task=200)
    p2 = _build_wire_payload(_result(run_id="same", per_task=tasks), max_per_task=200)
    assert [t.task_index for t in p1.per_task] == [t.task_index for t in p2.per_task]


def test_truncation_different_run_ids_different_samples():
    """Guard against global-seed regression (e.g. random.seed(0))."""
    tasks = _tasks(500)
    p1 = _build_wire_payload(_result(run_id="run-A", per_task=tasks), max_per_task=200)
    p2 = _build_wire_payload(_result(run_id="run-B", per_task=tasks), max_per_task=200)
    assert [t.task_index for t in p1.per_task] != [t.task_index for t in p2.per_task]


def test_empty_per_task_handled():
    p = _build_wire_payload(_result(per_task=[]), max_per_task=200)
    assert p.per_task == []
    assert p.per_task_total_count == 0
    assert p.per_task_truncated is False


def test_payload_excludes_free_form_in_per_task():
    p = _build_wire_payload(_result(per_task=_tasks(1)), max_per_task=200)
    dumped = p.per_task[0].model_dump()
    assert "prompt" not in dumped
    assert "response" not in dumped


def test_env_override_for_max_per_task(monkeypatch):
    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "5")
    # Re-import to pick up env at module load
    import importlib

    import maestro.benchmark.arbiter_report as ar

    importlib.reload(ar)
    try:
        assert ar.REPORT_MAX_PER_TASK == 5
    finally:
        # Restore default for subsequent tests
        monkeypatch.delenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", raising=False)
        importlib.reload(ar)


# ---------------------------------------------------------------------------
# Task 4.3 — _classify_error, ErrorClass, _ERROR_SEVERITY
# ---------------------------------------------------------------------------


from maestro.benchmark.arbiter_report import _classify_error  # noqa: E402
from maestro.coordination.arbiter_errors import (  # noqa: E402
    ArbiterContractError,
    ArbiterUnavailable,
)


def test_classify_timeout():
    assert _classify_error(TimeoutError()) == ("timeout", "report timed out")


def test_classify_contract_error_preserves_code_and_message():
    e = ArbiterContractError(-32602, "missing field")
    cls, msg = _classify_error(e)
    assert cls == "contract_break"
    assert "-32602" in msg
    assert "missing field" in msg


def test_classify_unavailable():
    e = ArbiterUnavailable("broken pipe")
    assert _classify_error(e) == ("unavailable", "arbiter unavailable")


def test_classify_unexpected_includes_type_name():
    e = ValueError("oops")
    cls, msg = _classify_error(e)
    assert cls == "unexpected"
    assert "ValueError" in msg
    assert "oops" in msg


def test_classify_dispatches_on_type_not_string():
    """Regression guard: an ArbiterUnavailable whose message contains 'timeout'
    must still classify as 'unavailable' (because dispatch is by type)."""
    e = ArbiterUnavailable("connection timeout after 30s")
    assert _classify_error(e) == ("unavailable", "arbiter unavailable")


# ---------------------------------------------------------------------------
# Task 4.4 — report_benchmark_to_arbiter happy + skipped paths
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter  # noqa: E402


@pytest.mark.asyncio
async def test_helper_returns_skipped_when_client_none():
    result = _result(run_id="skip")
    returned = await report_benchmark_to_arbiter(result, client=None)
    assert returned.report_status == "skipped"
    assert returned.report_error is None
    # Immutability: input unchanged (it was the default "skipped" anyway)
    assert result.report_status == "skipped"


@pytest.mark.asyncio
async def test_helper_returns_ok_on_created():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "created", "run_id": "x"}
    )
    result = _result(run_id="x")
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned.report_status == "ok"
    assert returned.report_error is None
    mock_client.report_benchmark_raw.assert_awaited_once()


@pytest.mark.asyncio
async def test_helper_returns_ok_on_duplicate():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "duplicate", "run_id": "x"}
    )
    result = _result(run_id="x")
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned.report_status == "ok"


@pytest.mark.asyncio
async def test_helper_returns_new_object_not_mutated():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "created", "run_id": "x"}
    )
    result = _result()
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned is not result
    assert result.report_status == "skipped"  # default — helper did NOT mutate input
    assert returned.report_status == "ok"
