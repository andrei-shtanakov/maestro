from datetime import datetime

import pytest

from maestro.execution.finalize import finalize_handle
from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)


class _Handle:
    def __init__(self, *, collect_raises=False):
        self.calls: list[str] = []
        self._collect_raises = collect_raises
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

    async def wait(self):
        self.calls.append("wait")
        return ExecutionResult(exit_code=0, output_log_path="/tmp/x")

    async def terminate(self, grace_seconds: float) -> None:
        pass

    async def kill(self) -> None:
        pass

    async def collect(self):
        self.calls.append("collect")
        if self._collect_raises:
            raise RuntimeError("conflict")
        return CollectResult(applied=True)

    async def cleanup(self):
        self.calls.append("cleanup")


@pytest.mark.anyio
async def test_collect_success_marks_between_phases_then_cleans():
    order: list[str] = []
    h = _Handle()
    fin = await finalize_handle(
        h,
        on_terminal=lambda: order.append("mark_terminal") or _noop(),
        on_collected=lambda: order.append("mark_collected") or _noop(),
    )
    assert h.calls == ["wait", "collect", "cleanup"]
    assert order == ["mark_terminal", "mark_collected"]
    assert fin.collect_succeeded and fin.cleaned


@pytest.mark.anyio
async def test_collect_failure_skips_cleanup_and_preserves():
    h = _Handle(collect_raises=True)
    fin = await finalize_handle(h, on_terminal=_acb(), on_collected=_acb())
    assert h.calls == ["wait", "collect"]  # NO cleanup
    assert not fin.collect_succeeded
    assert not fin.cleanup_attempted
    assert fin.collect_error == "conflict"


async def _noop():
    return None


def _acb():
    async def cb():
        return None

    return cb
