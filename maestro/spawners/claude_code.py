"""Claude Code spawner implementation.

This module provides the ClaudeCodeSpawner for running Claude Code
in headless mode with JSON output format.
"""

from pathlib import Path

from maestro._vendor import obs
from maestro.catalog import load_catalog, resolve_model, warn_on_model_status
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import Task
from maestro.spawners.base import AgentSpawner


_obs_log = obs.get_logger("maestro.spawners.claude_code")


class ClaudeCodeSpawner(AgentSpawner):
    """Spawner for Claude Code in headless mode.

    Runs Claude Code with --print and --output-format json flags
    for non-interactive execution. The model is resolved from the
    catalog; routed model wins, then ``MAESTRO_CLAUDE_MODEL``, then the
    catalog default.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "claude_code"

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        """Build a transport-agnostic ExecutionRequest for Claude Code.

        Mirrors the argv built by ``spawn()``; the backend opens the log
        file and spawns the process.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            run_id: Unique identifier for this run.
            retry_context: Error context from previous failed attempt.
            model: Routed model from the arbiter. Wins over
                ``MAESTRO_CLAUDE_MODEL`` and the catalog default
                (precedence: routed > env > catalog).

        Returns:
            Transport-agnostic execution request.
        """
        prompt = self.build_prompt(task, context, retry_context)
        catalog = load_catalog()
        resolved, source = resolve_model(
            model, "MAESTRO_CLAUDE_MODEL", "claude_code", catalog
        )
        _obs_log.info(
            "agent.model_resolved",
            harness="claude_code",
            model=resolved,
            source=source,
        )
        warn_on_model_status(resolved, source, catalog)
        return ExecutionRequest(
            run_id=run_id,
            argv=[
                "claude",
                "--model",
                resolved,
                "--print",
                "--output-format",
                "json",
                "-p",
                prompt,
            ],
            workdir=workdir,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
            required_tools=["claude"],
        )
