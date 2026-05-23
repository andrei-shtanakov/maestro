"""R-06b M4 contract tests — JSONSchema validation on Maestro side.

Schema lives at Maestro/_cowork_output/benchmark-contract/report_benchmark-v1.schema.json.
Both Maestro (this file) and arbiter (Rust tests/contract_test.rs)
validate against it. Schema is the single source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver

from maestro.benchmark.arbiter_report import _build_wire_payload
from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult


SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "_cowork_output"
    / "benchmark-contract"
    / "report_benchmark-v1.schema.json"
)


@pytest.fixture(scope="module")
def schema() -> dict:
    """Load the shared JSONSchema from disk."""
    with SCHEMA_PATH.open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def request_validator(schema: dict) -> Draft202012Validator:
    """Validator for the Request sub-schema, with full $ref resolution."""
    resolver = RefResolver.from_schema(schema)
    return Draft202012Validator(
        schema["definitions"]["Request"],
        resolver=resolver,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


@pytest.fixture(scope="module")
def response_validator(schema: dict) -> Draft202012Validator:
    """Validator for the Response sub-schema, with full $ref resolution."""
    resolver = RefResolver.from_schema(schema)
    return Draft202012Validator(
        schema["definitions"]["Response"],
        resolver=resolver,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def test_schema_file_exists() -> None:
    """Schema file must exist at the canonical contract path."""
    assert SCHEMA_PATH.exists(), f"schema file missing at {SCHEMA_PATH}"


def test_schema_is_valid_jsonschema(schema: dict) -> None:
    """Schema itself must be a valid JSON Schema (meta-validation)."""
    Draft202012Validator.check_schema(schema)


def test_pydantic_payload_validates_against_schema(
    request_validator: Draft202012Validator,
) -> None:
    """Canonical Pydantic-serialized payload must satisfy the Request sub-schema."""
    result = BenchmarkResult(
        run_id="r1",
        benchmark_id="b",
        agent_id="claude_code",
        score=0.85,
        score_components={"accuracy": 0.85},
        per_task=[
            BenchmarkTaskResult(
                task_index=0,
                prompt="p",
                response="r",
                duration_seconds=1.0,
                task_type="bugfix",
                score=0.9,
            )
        ],
        duration_seconds=10.0,
    )
    payload = _build_wire_payload(result, max_per_task=200)
    data = json.loads(payload.model_dump_json())
    errors = list(request_validator.iter_errors(data))
    assert not errors, f"validation errors: {[e.message for e in errors]}"


def test_missing_required_field_fails_validation(
    request_validator: Draft202012Validator,
) -> None:
    """Payload missing required fields must fail Request validation."""
    data = {"payload_version": "1.0.0", "run_id": "r1"}
    errors = list(request_validator.iter_errors(data))
    assert errors, "expected validation errors for missing required fields"


def test_response_created_validates(
    response_validator: Draft202012Validator,
) -> None:
    """Response with status=created must satisfy the Response sub-schema."""
    resp = {"status": "created", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert not errors, f"errors: {[e.message for e in errors]}"


def test_response_duplicate_validates(
    response_validator: Draft202012Validator,
) -> None:
    """Response with status=duplicate must satisfy the Response sub-schema."""
    resp = {"status": "duplicate", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert not errors


def test_response_unknown_status_fails(
    response_validator: Draft202012Validator,
) -> None:
    """Response with an unknown status value must fail validation."""
    resp = {"status": "weird", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert errors
