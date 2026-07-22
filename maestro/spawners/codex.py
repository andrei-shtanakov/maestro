"""Codex spawner implementation.

This module provides the CodexSpawner for running OpenAI Codex CLI
in non-interactive mode.
"""

from pathlib import Path

from maestro._vendor import obs
from maestro.catalog import load_catalog, resolve_model, warn_on_model_status
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import Task
from maestro.spawners.base import AgentSpawner


_obs_log = obs.get_logger("maestro.spawners.codex")


class CodexSpawner(AgentSpawner):
    """Spawner for OpenAI Codex CLI.

    Runs Codex non-interactively via ``codex exec`` with the
    ``workspace-write`` sandbox so it can edit files in the workdir without
    user interaction. The model is resolved from the catalog; routed model
    wins, then ``MAESTRO_CODEX_MODEL``, then the catalog default.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "codex_cli"

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
        """Build a transport-agnostic ExecutionRequest for Codex.

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
                ``MAESTRO_CODEX_MODEL`` and the catalog default
                (precedence: routed > env > catalog).

        Returns:
            Transport-agnostic execution request.
        """
        prompt = self.build_prompt(task, context, retry_context)
        catalog = load_catalog()
        resolved, source = resolve_model(
            model, "MAESTRO_CODEX_MODEL", "codex_cli", catalog
        )
        _obs_log.info(
            "agent.model_resolved",
            harness="codex_cli",
            model=resolved,
            source=source,
        )
        warn_on_model_status(resolved, source, catalog)
        return ExecutionRequest(
            run_id=run_id,
            argv=[
                "codex",
                "exec",
                "-m",
                resolved,
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                prompt,
            ],
            workdir=workdir,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
            required_tools=["codex"],
        )
