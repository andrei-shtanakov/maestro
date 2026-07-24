"""Single-owner finalization: reap, then persist-phase → collect → persist-phase
→ cleanup. DB transitions happen BETWEEN phases (via callbacks), so a crash in
the collect→cleanup window can never leave durable state that lies.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from maestro.execution.backend import TaskHandle
from maestro.execution.models import ExecutionResult


_Callback = Callable[[], Awaitable[None]] | None


@dataclass
class FinalizationResult:
    """Outcome of finalizing a handle."""

    execution: ExecutionResult
    collect_error: str | None = None
    cleanup_error: str | None = None
    collect_succeeded: bool = False
    cleanup_attempted: bool = False

    @property
    def cleaned(self) -> bool:
        return self.cleanup_attempted and self.cleanup_error is None


async def finalize_handle(
    handle: TaskHandle,
    *,
    on_terminal: _Callback = None,
    on_collected: _Callback = None,
) -> FinalizationResult:
    """Reap → persist terminal → collect → persist collected → cleanup.

    If ``collect()`` raises, finalization returns immediately WITHOUT running
    ``cleanup()`` — the remote/local workspace is preserved so a collect
    conflict can be inspected or retried rather than silently destroyed.
    """
    execution = await handle.wait()
    if on_terminal is not None:
        await on_terminal()
    try:
        await handle.collect()
    except Exception as e:
        # Collect failed/conflicted: DO NOT clean up — resources are preserved.
        return FinalizationResult(execution, collect_error=str(e))
    if on_collected is not None:
        await on_collected()
    cleanup_error: str | None = None
    try:
        await handle.cleanup()
    except Exception as e:
        cleanup_error = str(e)
    return FinalizationResult(
        execution,
        cleanup_error=cleanup_error,
        collect_succeeded=True,
        cleanup_attempted=True,
    )


class _Finalizable(Protocol):
    """Structural type for entities that own a handle and a finalize task."""

    handle: TaskHandle
    finalize_task: "asyncio.Task[FinalizationResult] | None"


def ensure_finalize_task(
    running: _Finalizable,
    *,
    on_terminal: _Callback = None,
    on_collected: _Callback = None,
) -> "asyncio.Task[FinalizationResult]":
    """Create the single finalization task for a running entity (idempotent)."""
    if running.finalize_task is None:
        running.finalize_task = asyncio.create_task(
            finalize_handle(
                running.handle, on_terminal=on_terminal, on_collected=on_collected
            )
        )
    return running.finalize_task
