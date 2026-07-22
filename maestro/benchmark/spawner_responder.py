"""R-06b M2 — adapter from Maestro ``AgentSpawner`` to ``AgentResponder``.

Lets ``BenchmarkRunner`` drive the existing CLI subprocess machinery
(claude_code, codex_cli, aider) without touching the ``AgentSpawner`` ABC.

Each ``respond(prompt)`` call:

1. Synthesises a minimal ``Task`` (no scope, no validation, no DB) so the
   spawner's task-oriented contract is satisfied.
2. Builds an ``ExecutionRequest`` via the wrapped spawner and runs it
   through an ``ExecutionBackend`` (``LocalBackend`` by default).
3. Awaits ``handle.wait()``, which internally honors
   ``request.timeout_seconds``. On timeout the backend kills the process
   and returns ``ExecutionResult(timed_out=True)``; the responder turns
   that into an empty response with ``error="timeout"``.
4. Parses tokens and cost from the captured log via
   ``maestro.cost_tracker``.
5. Returns the full log content as ``response.text`` — M2 punt; M3 will
   refine per-benchmark response extraction once the live ATP API shape
   is known.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from maestro.benchmark.models import AgentResponse
from maestro.cost_tracker import calculate_cost, parse_log
from maestro.execution.local import LocalBackend
from maestro.models import AgentType, Task, TaskStatus


if TYPE_CHECKING:
    from pathlib import Path

    from maestro.execution.backend import ExecutionBackend
    from maestro.spawners.base import AgentSpawner


logger = logging.getLogger(__name__)


class SpawnerResponder:
    """Adapts a ``maestro.spawners.AgentSpawner`` to the
    ``AgentResponder`` Protocol from ``maestro.benchmark.runner``."""

    def __init__(
        self,
        spawner: AgentSpawner,
        workdir: Path,
        log_dir: Path,
        timeout_seconds: float = 300.0,
        backend: ExecutionBackend | None = None,
    ) -> None:
        self._spawner = spawner
        self._workdir = workdir
        self._log_dir = log_dir
        self._timeout = timeout_seconds
        self._counter = 0
        self._backend: ExecutionBackend = (
            backend if backend is not None else LocalBackend()
        )

    @property
    def agent_id(self) -> str:
        return self._spawner.agent_type

    async def respond(self, prompt: str) -> AgentResponse:
        self._counter += 1
        task_id = f"benchmark-{self._counter}"
        log_file = self._log_dir / f"{task_id}.log"

        try:
            agent_enum = AgentType(self._spawner.agent_type)
        except ValueError:
            return AgentResponse(
                text="",
                error=f"unknown agent_type: {self._spawner.agent_type!r}",
            )

        task = Task(
            id=task_id,
            title=f"benchmark task {self._counter}",
            prompt=prompt,
            workdir=str(self._workdir),
            agent_type=agent_enum,
            status=TaskStatus.RUNNING,
        )

        request = self._spawner.build_request(
            task, "", self._workdir, log_file, task_id
        )
        request = request.model_copy(update={"timeout_seconds": self._timeout})

        handle = await self._backend.run(request)
        result = await handle.wait()

        if result.timed_out:
            logger.warning(
                "benchmark task %s exceeded timeout of %.1fs — killing",
                task_id,
                self._timeout,
            )
            return AgentResponse(text="", error="timeout")

        log_content = log_file.read_text() if log_file.exists() else ""
        usage = parse_log(log_content, agent_enum)
        total_tokens = usage.input_tokens + usage.output_tokens
        # A reported cost (opencode's part.cost, claude's total_cost_usd) wins
        # over the PRICING estimate and is preserved verbatim — including a
        # genuine 0.0 (a free model). Only an *estimated* zero (no tokens / no
        # pricing) collapses to None ("unknown"), since it is not an observation.
        if usage.cost_usd is not None:
            cost_wire = usage.cost_usd
        else:
            estimate = calculate_cost(usage, agent_enum)
            cost_wire = estimate or None

        if result.exit_code != 0:
            return AgentResponse(
                text="",
                tokens_used=total_tokens or None,
                cost_usd=cost_wire,
                error=f"exit code {result.exit_code}",
            )

        return AgentResponse(
            text=log_content,
            tokens_used=total_tokens or None,
            cost_usd=cost_wire,
        )
