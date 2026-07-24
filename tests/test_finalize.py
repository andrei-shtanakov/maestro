import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from maestro.execution.backend import TaskHandle
from maestro.execution.finalize import ensure_finalize_task, finalize_handle
from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)


class _FakeHandle:
    def __init__(self, *, collect_raises=False, cleanup_raises=False):
        self._collect_raises = collect_raises
        self._cleanup_raises = cleanup_raises
        self.cleaned_called = False
        self.ref = ExecutionHandleRef(
            backend_id="local",
            run_id="test-run",
            transport_ref="",
            started_at=datetime.now(),
        )

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return None

    async def wait(self) -> ExecutionResult:
        return ExecutionResult(exit_code=0, output_log_path=Path("/tmp/log"))

    async def terminate(self, grace_seconds: float) -> None:
        pass

    async def kill(self) -> None:
        pass

    async def collect(self) -> CollectResult:
        if self._collect_raises:
            raise RuntimeError("collect boom")
        return CollectResult(applied=False)

    async def cleanup(self) -> None:
        self.cleaned_called = True
        if self._cleanup_raises:
            raise RuntimeError("rm boom")


@pytest.mark.anyio
async def test_finalize_success_is_cleaned_and_keeps_exit_code():
    fin = await finalize_handle(_FakeHandle())
    assert fin.execution.exit_code == 0
    assert fin.cleaned is True
    assert fin.collect_error is None


@pytest.mark.anyio
async def test_collect_error_recorded_not_fatal_exit_code_untouched():
    handle = _FakeHandle(collect_raises=True)
    fin = await finalize_handle(handle)
    assert fin.execution.exit_code == 0  # business result untouched
    assert fin.collect_error is not None and "collect boom" in fin.collect_error
    assert handle.cleaned_called is True  # cleanup actually ran
    assert fin.cleaned is True  # cleanup succeeded


@pytest.mark.anyio
async def test_cleanup_error_marks_not_cleaned():
    handle = _FakeHandle(cleanup_raises=True)
    fin = await finalize_handle(handle)
    assert handle.cleaned_called is True
    assert fin.cleaned is False
    assert "rm boom" in (fin.cleanup_error or "")
    assert fin.execution.exit_code == 0


@dataclass
class _Running:
    handle: TaskHandle
    finalize_task: asyncio.Task | None = None


@pytest.mark.anyio
async def test_ensure_finalize_task_created_once():
    running = _Running(handle=_FakeHandle())
    t1 = ensure_finalize_task(running)
    t2 = ensure_finalize_task(running)
    assert t1 is t2  # exactly one task per entity
    fin = await asyncio.shield(t1)
    assert fin.cleaned is True
