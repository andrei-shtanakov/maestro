"""R-06b M4 e2e tests — real arbiter-mcp subprocess.

Auto-skip if the arbiter artifacts (binary + tree + config) are absent.
Mirrors R-05 pattern (tests/test_arbiter_real_subprocess.py).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maestro.benchmark import (
    BenchmarkResult,
    BenchmarkTaskResult,
    report_benchmark_to_arbiter,
)
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig


# ---------------------------------------------------------------------------
# Artifact discovery (mirrors R-05)
# ---------------------------------------------------------------------------


def _arbiter_repo_root() -> Path:
    """Locate the `arbiter` sibling repo."""
    return Path(__file__).resolve().parent.parent.parent / "arbiter"


def _arbiter_binary() -> Path:
    import os

    env = os.environ.get("MAESTRO_ARBITER_BIN")
    if env:
        return Path(env).resolve()
    return _arbiter_repo_root() / "target" / "release" / "arbiter-mcp"


def _arbiter_artifacts_present() -> bool:
    root = _arbiter_repo_root()
    binary = _arbiter_binary()
    tree = root / "models" / "agent_policy_tree.json"
    cfg_dir = root / "config"
    return binary.exists() and tree.exists() and cfg_dir.exists()


real_arbiter_only = pytest.mark.skipif(
    not _arbiter_artifacts_present(),
    reason=(
        "real arbiter binary or config missing; build with "
        "`cargo build --release --bin arbiter-mcp` in the arbiter repo. "
        "Override binary location with MAESTRO_ARBITER_BIN."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_arbiter_client(tmp_path: Path) -> object:
    """Spawn an arbiter-mcp subprocess pointing at a per-test temp DB.

    Yields a started, handshaken ArbiterClient ready for tool calls.
    Tears the subprocess down on exit even if the test raises.
    """
    root = _arbiter_repo_root()
    cfg = ArbiterClientConfig(
        binary_path=_arbiter_binary(),
        tree_path=root / "models" / "agent_policy_tree.json",
        config_dir=root / "config",
        db_path=tmp_path / "arbiter-bench-test.db",
        log_level="warn",
    )
    client = ArbiterClient(cfg)
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_result(run_id: str, per_task_n: int = 2) -> BenchmarkResult:
    """Build a minimal BenchmarkResult for e2e testing."""
    tasks = [
        BenchmarkTaskResult(
            task_index=i,
            prompt=f"p{i}",
            response=f"r{i}",
            duration_seconds=1.0 + i,
            task_type="bugfix",
            score=0.5 + i * 0.1,
        )
        for i in range(per_task_n)
    ]
    return BenchmarkResult(
        run_id=run_id,
        benchmark_id="e2e-bench",
        agent_id="claude_code",
        score=0.75,
        score_components={"accuracy": 0.75},
        per_task=tasks,
        duration_seconds=10.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@real_arbiter_only
@pytest.mark.anyio
async def test_report_benchmark_created_end_to_end(
    real_arbiter_client: ArbiterClient, tmp_path: Path
) -> None:
    """Happy path: report_benchmark_to_arbiter returns report_status='ok'
    and persists the row in the arbiter SQLite DB with expected aggregate
    columns (benchmark_id, agent_id, score, per_task counts).
    """
    result = _build_result(run_id="e2e-1")
    returned = await report_benchmark_to_arbiter(result, real_arbiter_client)

    assert returned.report_status == "ok", (
        f"got report_status={returned.report_status!r}: {returned.report_error}"
    )
    assert returned.report_error is None

    # Locate the DB the fixture wrote to
    db_path = tmp_path / "arbiter-bench-test.db"
    assert db_path.exists(), "arbiter DB file not created"

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT benchmark_id, agent_id, score, per_task_total_count, "
            "per_task_truncated FROM benchmark_runs WHERE run_id=?",
            ("e2e-1",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "no row found in benchmark_runs for run_id='e2e-1'"
    benchmark_id, agent_id, score, per_task_total_count, per_task_truncated = row
    assert benchmark_id == "e2e-bench"
    assert agent_id == "claude_code"
    assert abs(score - 0.75) < 1e-6, f"expected score≈0.75, got {score}"
    assert per_task_total_count == 2
    assert per_task_truncated == 0  # 2 tasks well below cap


@real_arbiter_only
@pytest.mark.anyio
async def test_report_benchmark_duplicate_end_to_end(
    real_arbiter_client: ArbiterClient, tmp_path: Path
) -> None:
    """Same run_id twice → both report_status='ok', exactly 1 row.

    Validates the ON CONFLICT DO NOTHING contract for idempotency:
    sequential duplicate report_benchmark calls with the same run_id
    both return ok status, but only one row persists in the DB.
    """
    client = real_arbiter_client
    result = _build_result(run_id="e2e-dup")

    r1 = await report_benchmark_to_arbiter(result, client)
    r2 = await report_benchmark_to_arbiter(result, client)

    assert r1.report_status == "ok"
    assert r2.report_status == "ok"  # duplicate still maps to ok status
    assert r1.report_error is None
    assert r2.report_error is None

    # Verify exactly 1 row in SQLite.
    db_path = tmp_path / "arbiter-bench-test.db"
    assert db_path.exists(), "arbiter DB file not created"

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM benchmark_runs WHERE run_id=?", ("e2e-dup",)
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 1, f"duplicate must not insert a second row (got {count})"
