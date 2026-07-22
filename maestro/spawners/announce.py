"""Announce spawner implementation.

This module provides the AnnounceSpawner, a notification-only spawner
that logs the task details without running any AI agent. Useful for
milestone markers, manual tasks, or notification-only entries in the DAG.
"""

from pathlib import Path

from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import Task
from maestro.spawners.base import AgentSpawner


class AnnounceSpawner(AgentSpawner):
    """Notification-only spawner that logs task details and exits.

    This spawner does not invoke any external AI agent. Instead, it
    writes the task prompt to the log file and exits successfully.
    Use it for announce-only tasks, milestone markers, or manual
    coordination points in the task DAG.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "announce"

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,  # noqa: ARG002 - kept for API consistency
    ) -> ExecutionRequest:
        """Build a transport-agnostic ExecutionRequest for announcements.

        Mirrors the argv built by ``spawn()``; the backend opens the log
        file and spawns the process. ``echo`` is a shell builtin/coreutil
        so it is not listed in ``required_tools``.

        Args:
            task: Task to announce.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write announcement output.
            run_id: Unique identifier for this run.
            retry_context: Error context from previous failed attempt.
            model: Accepted for interface parity; unused (no model concept).

        Returns:
            Transport-agnostic execution request.
        """
        prompt = self.build_prompt(task, context, retry_context)
        return ExecutionRequest(
            run_id=run_id,
            argv=["echo", prompt],
            workdir=workdir,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
        )
