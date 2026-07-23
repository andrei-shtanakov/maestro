"""Scheduler <-> execution_handles wiring for non-local (docker) dispatch.

Task 16: `_spawn_task` mints an `execution_id` for non-local backends,
persists it via `Database.start_execution` (atomic CAS + insert), and
dispatches the already-committed transition. `_monitor_running_tasks`
then drives the handle's state to `terminal` -> `cleaned` on completion.

No real docker daemon is involved — the backend is a fake that mimics the
`ExecutionBackend`/`TaskHandle` protocol shape used elsewhere in the
scheduler test suite (see `tests/fakes/fake_execution_backend.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig
from tests.fakes.fake_execution_backend import FakeExecutionBackend


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class FakeDockerBackend(FakeExecutionBackend):
    """Same fake handle behavior as `FakeExecutionBackend`, but `id="docker"`.

    `_spawn_task` branches on `backend.id != "local"`, so this is enough to
    exercise the mint-persist-dispatch path without a real docker backend.
    """

    id = "docker"


def _docker_spawner(exit_code: int = 0) -> MagicMock:
    """A MagicMock spawner returning a real ExecutionRequest (no backend_id
    set — `_spawn_task` stamps that on after resolving the backend).
    """
    spawner = MagicMock()
    spawner.agent_type = "claude_code"
    spawner.is_available.return_value = True
    spawner.can_build_request.return_value = True
    spawner.build_request.return_value = ExecutionRequest(
        run_id="r",
        argv=["true"],
        workdir=Path("/tmp"),
        log_path=Path("/tmp/fake-docker-wiring.log"),
        collect=CollectPolicy(mode="none"),
        labels={"fake_return_code": str(exit_code)},
    )
    return spawner


@pytest.fixture
async def scheduler_docker_env(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Scheduler, Database, str], None]:
    """Build a Scheduler with one READY task backed by a fake docker backend.

    Mirrors the direct-Task construction pattern used in
    `tests/test_scheduler_arbiter_integration.py::_setup_task_and_scheduler`.
    """
    db = Database(tmp_path / "s.db")
    await db.connect()

    task_id = "docker-task-1"
    task = Task(
        id=task_id,
        title="Docker task",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.READY,
        max_retries=2,
        backend="docker",
    )
    await db.create_task(task)

    (tmp_path / "logs").mkdir(exist_ok=True)
    scheduler = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"claude_code": _docker_spawner(exit_code=0)},
        config=SchedulerConfig(
            max_concurrent=3,
            workdir=tmp_path,
            log_dir=tmp_path / "logs",
        ),
    )
    # Bypass BackendResolver._build (which requires a real execution.docker
    # config) and hand back the fake docker backend directly for any name —
    # this test only ever resolves "docker".
    fake_backend = FakeDockerBackend()
    scheduler._backends.resolve = lambda _name: fake_backend  # type: ignore[method-assign]

    try:
        yield scheduler, db, task_id
    finally:
        await db.close()


@pytest.fixture
async def scheduler_local_env(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Scheduler, Database, str], None]:
    """Same as `scheduler_docker_env` but the task uses the local backend."""
    db = Database(tmp_path / "s.db")
    await db.connect()

    task_id = "local-task-1"
    task = Task(
        id=task_id,
        title="Local task",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.READY,
        max_retries=2,
        backend="local",
    )
    await db.create_task(task)

    (tmp_path / "logs").mkdir(exist_ok=True)
    scheduler = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"claude_code": _docker_spawner(exit_code=0)},
        config=SchedulerConfig(
            max_concurrent=3,
            workdir=tmp_path,
            log_dir=tmp_path / "logs",
        ),
    )
    fake_local = FakeExecutionBackend()
    fake_local.id = "local"  # type: ignore[misc]  # exercise the local branch
    scheduler._backends.resolve = lambda _name: fake_local  # type: ignore[method-assign]

    try:
        yield scheduler, db, task_id
    finally:
        await db.close()


@pytest.mark.anyio
async def test_docker_task_persists_and_cleans_execution_handle(
    scheduler_docker_env: tuple[Scheduler, Database, str],
) -> None:
    sched, db, task_id = scheduler_docker_env

    started = await sched._spawn_task(task_id)
    assert started is True

    rows = await db.get_open_execution_handles()
    docker_rows = [r for r in rows if r["entity_id"] == task_id]
    assert len(docker_rows) == 1
    assert docker_rows[0]["backend_id"] == "docker"
    assert docker_rows[0]["state"] in ("prepared", "running")

    running_task = sched._running_tasks[task_id]
    assert running_task.execution_id is not None

    task = await db.get_task(task_id)
    assert task.status is TaskStatus.RUNNING

    # Drive one monitor tick to completion (fake handle polls exit 0
    # immediately).
    await sched._monitor_running_tasks()

    assert task_id not in sched._running_tasks
    open_rows = await db.get_open_execution_handles()
    assert all(r["entity_id"] != task_id for r in open_rows)

    done_task = await db.get_task(task_id)
    assert done_task.status is TaskStatus.DONE


@pytest.mark.anyio
async def test_local_task_unaffected_by_execution_handle_wiring(
    scheduler_local_env: tuple[Scheduler, Database, str],
) -> None:
    sched, db, task_id = scheduler_local_env

    started = await sched._spawn_task(task_id)
    assert started is True

    running_task = sched._running_tasks[task_id]
    assert running_task.execution_id is None

    task = await db.get_task(task_id)
    assert task.status is TaskStatus.RUNNING

    rows = await db.get_open_execution_handles()
    assert all(r["entity_id"] != task_id for r in rows)

    await sched._monitor_running_tasks()

    done_task = await db.get_task(task_id)
    assert done_task.status is TaskStatus.DONE
    rows_after = await db.get_open_execution_handles()
    assert all(r["entity_id"] != task_id for r in rows_after)
