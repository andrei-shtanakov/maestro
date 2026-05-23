"""R-06b M4 — Arbiter feedback wiring.

Projects a BenchmarkResult into a wire payload for the arbiter
report_benchmark MCP tool, and delivers it via ArbiterClient.

The full helper (``report_benchmark_to_arbiter``) is added in subsequent
tasks (4.2-4.6). This module currently exposes the wire projection
type ``WireTaskResult`` and the ``_bucket_error`` helper.

Design: docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from maestro._vendor import obs
from maestro.coordination.arbiter_errors import (
    ArbiterContractError,
    ArbiterUnavailable,
)


_obs_log = obs.get_logger("maestro.benchmark.arbiter_report")


if TYPE_CHECKING:
    from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult


@runtime_checkable
class _ArbiterClientLike(Protocol):
    """Structural protocol for duck-typed arbiter client.

    Keeps the benchmark layer independent of the coordination layer —
    any object with ``report_benchmark_raw(dict) -> awaitable`` satisfies
    this protocol. ``ArbiterClient`` from ``maestro.coordination`` fulfils
    it without explicit registration.
    """

    async def report_benchmark_raw(self, payload: dict) -> dict: ...


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


# ---------------------------------------------------------------------------
# Task 4.2 — ReportBenchmarkPayload, _sample_per_task, _build_wire_payload
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PER_TASK = 200
REPORT_MAX_PER_TASK: int = int(
    os.getenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", _DEFAULT_MAX_PER_TASK)
)


class ReportBenchmarkPayload(BaseModel):
    """Wire payload for the arbiter ``report_benchmark`` MCP tool (v1.0.0).

    Frozen and ``extra="forbid"`` — any drift requires a
    ``payload_version`` bump + contract test update on both sides.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    benchmark_id: str
    agent_id: str
    ts: str  # RFC3339 UTC
    score: float
    score_components: dict[str, float]
    total_tokens: int | None
    total_cost_usd: float | None
    duration_seconds: float
    per_task: list[WireTaskResult]
    per_task_total_count: int
    per_task_truncated: bool


def _sample_per_task(
    tasks: list[BenchmarkTaskResult],
    cap: int,
    run_id: str,
) -> tuple[list[WireTaskResult], bool]:
    """Project tasks → WireTaskResult, applying deterministic sample if oversize.

    Seed = ``run_id`` so re-runs with the same id pick the same sub-sample
    (reproducible debug). Random sample (not head-N) avoids systematic
    bias when benchmarks order tasks by difficulty.
    """
    if len(tasks) <= cap:
        return [WireTaskResult.from_domain(t) for t in tasks], False
    rng = random.Random(run_id)
    sampled = sorted(rng.sample(tasks, cap), key=lambda t: t.task_index)
    return [WireTaskResult.from_domain(t) for t in sampled], True


def _build_wire_payload(
    result: BenchmarkResult,
    max_per_task: int,
) -> ReportBenchmarkPayload:
    """Project a domain ``BenchmarkResult`` into a wire ``ReportBenchmarkPayload``."""
    per_task_wire, truncated = _sample_per_task(
        result.per_task, max_per_task, result.run_id
    )
    ts_str = result.ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ReportBenchmarkPayload(
        payload_version="1.0.0",
        run_id=result.run_id,
        benchmark_id=result.benchmark_id,
        agent_id=result.agent_id,
        ts=ts_str,
        score=result.score,
        score_components=dict(result.score_components),
        total_tokens=result.total_tokens,
        total_cost_usd=result.total_cost_usd,
        duration_seconds=result.duration_seconds,
        per_task=per_task_wire,
        per_task_total_count=len(result.per_task),
        per_task_truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Task 4.3 — _classify_error, ErrorClass, _ERROR_SEVERITY
# ---------------------------------------------------------------------------

ErrorClass = Literal["unavailable", "timeout", "contract_break", "unexpected"]

_ERROR_SEVERITY: dict[ErrorClass, Literal["warning", "error"]] = {
    "unavailable": "warning",  # transient
    "timeout": "warning",  # transient
    "contract_break": "error",  # vendored drift / payload bug — must fix
    "unexpected": "error",  # catch-all → tracking
}


def _classify_error(exc: BaseException) -> tuple[ErrorClass, str]:
    """Normalize an exception into (class, short_msg). isinstance dispatch.

    Single source of truth for both ``obs.emit`` error_class and the
    ``BenchmarkResult.report_error`` message. NEVER use string-match
    on exc.args / str(exc) — that's the bug class this avoids.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", "report timed out"
    if isinstance(exc, ArbiterContractError):
        return "contract_break", f"{exc.code}: {exc.message}"
    if isinstance(exc, ArbiterUnavailable):
        return "unavailable", "arbiter unavailable"
    return "unexpected", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Task 4.4 — report_benchmark_to_arbiter (happy + skipped paths)
# Error paths (failed/contract_break) are added in Task 4.5.
# obs.emit instrumentation is added in Task 4.6.
# ---------------------------------------------------------------------------

REPORT_TIMEOUT_S = 30.0


async def report_benchmark_to_arbiter(
    result: BenchmarkResult,
    client: _ArbiterClientLike | None,
    *,
    max_per_task: int = REPORT_MAX_PER_TASK,
) -> BenchmarkResult:
    """Send a benchmark result to arbiter; return updated copy with report_status set.

    Returns a NEW ``BenchmarkResult`` with ``report_status`` set —
    never mutates the input. Helper never raises except for
    ``asyncio.CancelledError`` (a ``BaseException`` that must propagate).

    Emits 5 distinct obs events (one per outcome class):
    - ``benchmark.report.skipped`` (info) — client=None
    - ``benchmark.report.succeeded`` (info) — RPC ok, new row created
    - ``benchmark.report.duplicate`` (info) — RPC ok, idempotency
    - ``benchmark.report.failed`` (warning|error) — transient or unexpected failure
    - ``benchmark.report.contract_break`` (error) — JSON-RPC contract drift

    The contract_break case has its own event NAME (not just severity)
    so alerting rules can match by name directly.
    """
    event_attrs = {
        "run_id": result.run_id,
        "benchmark_id": result.benchmark_id,
        "agent_id": result.agent_id,
    }
    if client is None:
        _obs_log.info("benchmark.report.skipped", **event_attrs)
        return result.model_copy(update={"report_status": "skipped"})

    with obs.span("benchmark.report", **event_attrs):
        try:
            payload = _build_wire_payload(result, max_per_task)
            response = await asyncio.wait_for(
                client.report_benchmark_raw(payload.model_dump(mode="json")),
                timeout=REPORT_TIMEOUT_S,
            )
            status = response.get("status") if isinstance(response, dict) else None
            if status == "duplicate":
                _obs_log.info("benchmark.report.duplicate", **event_attrs)
            else:
                _obs_log.info(
                    "benchmark.report.succeeded", score=result.score, **event_attrs
                )
            return result.model_copy(update={"report_status": "ok"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_class, details = _classify_error(exc)
            severity = _ERROR_SEVERITY[error_class]
            event_name = (
                "benchmark.report.contract_break"
                if error_class == "contract_break"
                else "benchmark.report.failed"
            )
            log_method = _obs_log.error if severity == "error" else _obs_log.warning
            log_method(
                event_name,
                error_class=error_class,
                error=details,
                severity=severity,
                **event_attrs,
            )
            return result.model_copy(
                update={
                    "report_status": "failed",
                    "report_error": f"{error_class}: {details}",
                }
            )
