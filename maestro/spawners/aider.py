"""Aider spawner implementation.

This module provides the AiderSpawner for running the Aider AI pair
programming tool in non-interactive mode.
"""

from pathlib import Path

from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import Task
from maestro.spawners.base import AgentSpawner


class AiderSpawner(AgentSpawner):
    """Spawner for Aider AI pair programming tool.

    Runs Aider in non-interactive (yes) mode with auto-commits
    disabled so the orchestrator controls git operations.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "aider"

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
        """Build a transport-agnostic ExecutionRequest for Aider.

        Mirrors the argv built by ``spawn()``; the backend opens the log
        file and spawns the process. Scope files are appended so Aider
        knows which files to edit.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            run_id: Unique identifier for this run.
            retry_context: Error context from previous failed attempt.
            model: Accepted for interface parity; unused (no model concept).

        Returns:
            Transport-agnostic execution request.
        """
        prompt = self.build_prompt(task, context, retry_context)

        argv: list[str] = [
            "aider",
            "--yes-always",
            "--no-auto-commits",
            "--message",
            prompt,
        ]
        if task.scope:
            argv.extend(task.scope)

        return ExecutionRequest(
            run_id=run_id,
            argv=argv,
            workdir=workdir,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
            required_tools=["aider"],
        )
