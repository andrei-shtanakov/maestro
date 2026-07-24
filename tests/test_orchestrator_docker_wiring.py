"""Orchestrator <-> execution_handles wiring for non-local (docker) dispatch.

Task 17: `_spawn_workstream` mints an `execution_id` for non-local backends,
persists it via `Database.start_execution` (atomic CAS + insert), and
dispatches the already-committed transition directly (mirrors Task 16's
`Scheduler._spawn_task`). `_monitor_running` then finalizes the handle
exactly once and drives the execution_handles row `terminal` -> `cleaned`
on completion.

No real docker daemon or spec-runner process is involved — the backend is a
fake mirroring the `ExecutionBackend`/`TaskHandle` protocol shape used by
`tests/test_orchestrator.py::FakeOrchestratorBackend`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from maestro.database import Database
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.models import OrchestratorConfig, Workstream, WorkstreamStatus
from maestro.orchestrator import Orchestrator
from tests.fakes.fake_execution_backend import FakeTaskHandle


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


class FakeBackend:
    """`ExecutionBackend` double with a caller-supplied `id`.

    `_spawn_workstream` branches on `backend.id != "local"`, so a plain
    `id` swap is enough to exercise either the docker-mint-persist path or
    the unchanged local path, without a real docker/subprocess backend.
    """

    def __init__(
        self,
        backend_id: str,
        *,
        exit_code: int = 0,
        pid: int = 1,
        reachable: bool = True,
    ) -> None:
        self.id = backend_id
        self.exit_code = exit_code
        self.pid = pid
        self.reachable = reachable
        self.created_handles: list[FakeTaskHandle] = []
        self.requests: list[ExecutionRequest] = []

    async def healthcheck(self) -> BackendHealth:
        if self.reachable:
            return BackendHealth(reachable=True)
        return BackendHealth(reachable=False, detail="DOCKER_HOST is remote")

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        del req
        return CapabilityResult(ok=True)

    async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
        self.requests.append(req)
        handle = FakeTaskHandle(exit_code=self.exit_code, pid=self.pid)
        self.created_handles.append(handle)
        return handle

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        raise NotImplementedError("not exercised by these tests")


def _orch_config(tmp_path: Path) -> OrchestratorConfig:
    """Minimal config: auto_pr off keeps `_handle_success` from touching
    GitHub; `repo_path` is a real (non-git) directory so `_merge_into_base`'s
    subprocess calls run (and fail closed to NEEDS_REVIEW) instead of
    raising `FileNotFoundError` from a nonexistent cwd."""
    return OrchestratorConfig(
        project="test-project",
        repo_url="https://github.com/test/repo",
        repo_path=str(tmp_path),
        workspace_base=str(tmp_path / "ws"),
        max_concurrent=2,
        auto_pr=False,
    )


def _seed_workstream(workstream_id: str) -> Workstream:
    """Empty `scope` skips the scope gate (H-6/WS-006) so no real git repo
    is needed for the completion path this module drives."""
    return Workstream(
        id=workstream_id,
        title=workstream_id,
        description="d",
        scope=[],
        branch=f"feature/{workstream_id}",
        status=WorkstreamStatus.READY,
    )


@pytest.fixture
async def orch_env(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Orchestrator, Database, str], None]:
    """Real (aiosqlite) `Database` + mocked workspace/decomposer/pr_manager.

    `self._gates` stays `None` (no `gates` config) so the ex-ante/ex-post
    gates are trivially satisfied; combined with the empty-scope seed
    workstream, `_handle_success`'s scope gate is skipped too.
    """
    db = Database(tmp_path / "o.db")
    await db.connect()

    workstream_id = "z1"
    await db.create_workstream(_seed_workstream(workstream_id))

    workspace = tmp_path / "ws" / workstream_id
    workspace.mkdir(parents=True)

    workspace_mgr = MagicMock()
    workspace_mgr.workspace_exists = MagicMock(return_value=True)
    workspace_mgr.get_workspace_path = MagicMock(return_value=workspace)
    workspace_mgr.setup_spec_runner = MagicMock()

    decomposer = MagicMock()

    async def _generate_spec(workstream, workspace_path, *, on_pid=None):
        del workstream, workspace_path, on_pid

    decomposer.generate_spec = _generate_spec

    pr_manager = MagicMock()

    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    orch = Orchestrator(
        db=db,
        workspace_mgr=workspace_mgr,
        decomposer=decomposer,
        pr_manager=pr_manager,
        config=_orch_config(tmp_path),
        log_dir=log_dir,
    )

    with patch("maestro.orchestrator.ensure_harness_excludes"):
        try:
            yield orch, db, workstream_id
        finally:
            await db.close()


@pytest.mark.anyio
async def test_docker_workstream_persists_and_cleans_execution_handle(
    orch_env: tuple[Orchestrator, Database, str],
) -> None:
    orch, db, workstream_id = orch_env
    fake_backend = FakeBackend("docker", exit_code=0, pid=4242)
    orch._backends.resolve = lambda _name: fake_backend  # type: ignore[method-assign]

    await orch._generate_and_launch(workstream_id)

    rows = await db.get_open_execution_handles()
    docker_rows = [r for r in rows if r["entity_id"] == workstream_id]
    assert len(docker_rows) == 1
    assert docker_rows[0]["entity_kind"] == "workstream"
    assert docker_rows[0]["backend_id"] == "docker"
    assert docker_rows[0]["state"] in ("prepared", "running")

    running = orch._running[workstream_id]
    assert running.execution_id is not None

    workstream = await db.get_workstream(workstream_id)
    assert workstream.status is WorkstreamStatus.RUNNING

    # Drive one monitor tick to completion (fake handle polls exit 0
    # immediately).
    await orch._monitor_running()

    assert workstream_id not in orch._running
    open_rows = await db.get_open_execution_handles()
    assert all(r["entity_id"] != workstream_id for r in open_rows)


@pytest.mark.anyio
async def test_docker_workstream_healthcheck_unreachable_routes_needs_review(
    orch_env: tuple[Orchestrator, Database, str],
) -> None:
    """An unreachable non-local backend must fail fast before spawning.

    `_spawn_workstream` routes READY -> NEEDS_REVIEW instead of RUNNING, and
    never mints an execution_id or persists an execution_handles row, so a
    docker workstream never dispatches against an unreachable/remote daemon.
    """
    orch, db, workstream_id = orch_env
    fake_backend = FakeBackend("docker", exit_code=0, pid=4242, reachable=False)
    orch._backends.resolve = lambda _name: fake_backend  # type: ignore[method-assign]

    await orch._generate_and_launch(workstream_id)

    workstream = await db.get_workstream(workstream_id)
    assert workstream.status is WorkstreamStatus.NEEDS_REVIEW
    assert workstream.error_message is not None
    assert "not reachable" in workstream.error_message

    assert workstream_id not in orch._running
    rows = await db.get_open_execution_handles()
    assert all(r["entity_id"] != workstream_id for r in rows)
    assert fake_backend.created_handles == []


@pytest.mark.anyio
async def test_local_workstream_unaffected_by_execution_handle_wiring(
    orch_env: tuple[Orchestrator, Database, str],
) -> None:
    orch, db, workstream_id = orch_env
    fake_backend = FakeBackend("local", exit_code=0, pid=4242)
    orch._backends.resolve = lambda _name: fake_backend  # type: ignore[method-assign]

    await orch._generate_and_launch(workstream_id)

    running = orch._running[workstream_id]
    assert running.execution_id is None

    workstream = await db.get_workstream(workstream_id)
    assert workstream.status is WorkstreamStatus.RUNNING

    rows = await db.get_open_execution_handles()
    assert all(r["entity_id"] != workstream_id for r in rows)

    await orch._monitor_running()

    assert workstream_id not in orch._running
    rows_after = await db.get_open_execution_handles()
    assert all(r["entity_id"] != workstream_id for r in rows_after)
