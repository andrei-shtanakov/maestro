"""R-06b M4 — Arbiter feedback wiring.

Projects a BenchmarkResult into a wire payload for the arbiter
report_benchmark MCP tool, and delivers it via ArbiterClient.

The full helper (``report_benchmark_to_arbiter``) is added in subsequent
tasks (4.2-4.6). This module currently exposes the wire projection
type ``WireTaskResult`` and the ``_bucket_error`` helper.

Design: docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict


if TYPE_CHECKING:
    from maestro.benchmark.models import BenchmarkTaskResult


ErrorClassBucket = Literal["timeout", "crash", "test_failure", "other"]


def _bucket_error(msg: str | None) -> ErrorClassBucket | None:
    """Classify a free-form error string into a bounded enum.

    Wire payload only carries the bucket label, not the raw message
    (free-form text stays in the domain BenchmarkTaskResult and local
    logs). This keeps the arbiter side queryable while bounding blob
    growth.
    """
    if msg is None:
        return None
    lower = msg.lower()
    if "timeout" in lower:
        return "timeout"
    if "crash" in lower or "exited" in lower or "killed" in lower:
        return "crash"
    if "test" in lower and ("fail" in lower or "error" in lower):
        return "test_failure"
    return "other"


class WireTaskResult(BaseModel):
    """Projection of BenchmarkTaskResult for arbiter persistence.

    Excludes free-form fields (``prompt``, ``response``) — they live
    only in the in-memory domain object and Maestro logs. Adding a
    field here requires a ``payload_version`` bump + contract-test
    update on both sides.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_index: int
    task_type: str | None
    score: float | None
    tokens_used: int | None
    duration_seconds: float
    error_class: ErrorClassBucket | None

    @classmethod
    def from_domain(cls, task: BenchmarkTaskResult) -> WireTaskResult:
        """Build a WireTaskResult from a domain BenchmarkTaskResult."""
        return cls(
            task_index=task.task_index,
            task_type=task.task_type,
            score=task.score,
            tokens_used=task.tokens_used,
            duration_seconds=task.duration_seconds,
            error_class=_bucket_error(task.error),
        )
