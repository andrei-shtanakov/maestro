"""Single-owner finalization: reap, collect, cleanup — as a structured result.

The monitor (not this helper) drives execution_handles state and never lets a
resource-cleanup fault rewrite the agent's business exit code.
"""

from dataclasses import dataclass

from maestro.execution.backend import TaskHandle
from maestro.execution.models import ExecutionResult


@dataclass
class FinalizationResult:
    """Outcome of finalizing a handle."""

    execution: ExecutionResult
    collect_error: str | None = None
    cleanup_error: str | None = None

    @property
    def cleaned(self) -> bool:
        return self.cleanup_error is None


async def finalize_handle(handle: TaskHandle) -> FinalizationResult:
    """Reap the handle, then collect + cleanup, capturing (not raising) faults."""
    execution = await handle.wait()
    collect_error: str | None = None
    cleanup_error: str | None = None
    try:
        await handle.collect()
    except Exception as e:
        collect_error = str(e)
    try:
        await handle.cleanup()
    except Exception as e:
        cleanup_error = str(e)
    return FinalizationResult(execution, collect_error, cleanup_error)
