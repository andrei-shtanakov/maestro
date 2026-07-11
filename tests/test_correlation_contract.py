"""WorkCorrelation v1: schema, projections, transitions, builders."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import jsonschema
import pytest

from maestro.correlation import (
    PROJECTIONS,
    TERMINAL,
    UNIVERSAL_EXITS,
    CommonStatus,
    for_arbiter_outcome,
    for_maestro_task,
    for_spec_task,
    for_workstream,
    is_valid_transition,
    project_status,
)
from maestro.models import ExecutorTaskStatus, TaskStatus, WorkstreamStatus


_CONTRACT_DIR = Path(__file__).parent.parent / "contracts" / "work-correlation"
_SCHEMA = json.loads((_CONTRACT_DIR / "schema.json").read_text())


# ---------------------------------------------------------------- schema


@pytest.mark.parametrize(
    "fixture", sorted((_CONTRACT_DIR / "fixtures").glob("*.json"), key=str)
)
def test_golden_fixtures_validate(fixture: Path) -> None:
    record = json.loads(fixture.read_text())
    jsonschema.validate(record, _SCHEMA)


def test_schema_enum_matches_reference_impl() -> None:
    schema_enum = set(_SCHEMA["properties"]["status"]["enum"])
    assert schema_enum == {s.value for s in CommonStatus}


def test_builders_produce_schema_valid_records() -> None:
    records = [
        for_maestro_task(
            "t-1",
            TaskStatus.DONE,
            pipeline_id="01KX8V7Z9DHBKYWGSN2KTWM8AB",
            trace_id="b11462b5fa030af9b11462b5fa030af9",
            ts="2026-07-11T15:02:46+00:00",
        ),
        for_workstream("ws-1", WorkstreamStatus.PR_CREATED),
        for_spec_task("ws-1", "workstreams/ws-1/spec", "TASK-1", "success"),
        for_arbiter_outcome("t-1", "timeout"),
    ]
    for record in records:
        jsonschema.validate(record.model_dump(), _SCHEMA)


# ----------------------------------------------------------- projections


@pytest.mark.parametrize(
    ("vocabulary", "enum"),
    [
        ("maestro.task", TaskStatus),
        ("maestro.workstream", WorkstreamStatus),
        ("spec-runner.task", ExecutorTaskStatus),
    ],
)
def test_projection_total_over_source_enum(vocabulary: str, enum: type) -> None:
    """Every source-enum member must project — drift fails loudly here."""
    for member in enum:
        assert project_status(vocabulary, str(member)) in CommonStatus


def test_projection_surjective() -> None:
    """Every common status is reachable from at least one source status."""
    reachable = {common for table in PROJECTIONS.values() for common in table.values()}
    assert reachable == set(CommonStatus)


def test_unknown_vocabulary_and_status_fail_loudly() -> None:
    with pytest.raises(ValueError, match="unknown status vocabulary"):
        project_status("maestro.nope", "done")
    # the exact live-drift case that motivated source_status (Maestro #65):
    with pytest.raises(ValueError, match="interrupted"):
        project_status("arbiter.outcome", "interrupted")


def test_human_wait_states_project_to_needs_review() -> None:
    assert (
        project_status("maestro.task", TaskStatus.AWAITING_APPROVAL)
        == CommonStatus.NEEDS_REVIEW
    )
    assert (
        project_status("maestro.workstream", WorkstreamStatus.PR_CREATED)
        == CommonStatus.NEEDS_REVIEW
    )


# ------------------------------------------------------------ transitions


def test_universal_exits_from_any_non_terminal() -> None:
    for status in CommonStatus:
        for exit_status in UNIVERSAL_EXITS:
            expected = status not in TERMINAL
            assert is_valid_transition(status, exit_status) is expected


def test_terminal_states_have_no_exits() -> None:
    for terminal in TERMINAL:
        assert all(not is_valid_transition(terminal, s) for s in CommonStatus)


def test_recovery_paths() -> None:
    assert is_valid_transition(CommonStatus.FAILED, CommonStatus.PENDING)
    assert is_valid_transition(CommonStatus.NEEDS_REVIEW, CommonStatus.PENDING)
    assert not is_valid_transition(CommonStatus.DONE, CommonStatus.PENDING)


def test_happy_path() -> None:
    path = [
        CommonStatus.PENDING,
        CommonStatus.RUNNING,
        CommonStatus.DONE,
    ]
    for current, new in itertools.pairwise(path):
        assert is_valid_transition(current, new)


# --------------------------------------------------------------- builders


def test_spec_task_bridge_derives_child_key() -> None:
    record = for_spec_task("ws-1", "workstreams/ws-1/spec", "TASK-042", "success")
    assert record.work_item_id == "ws-1/TASK-042"
    assert record.parent_work_item_id == "ws-1"
    assert record.source_locator == "workstreams/ws-1/spec"
    assert record.source_status == "success"  # verbatim
    assert record.status == CommonStatus.DONE  # projected
