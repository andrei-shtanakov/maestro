import pytest
from pydantic import ValidationError

from maestro.benchmark.arbiter_report import WireTaskResult
from maestro.benchmark.models import BenchmarkTaskResult


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
