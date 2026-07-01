"""Codex spawner implementation.

This module provides the CodexSpawner for running OpenAI Codex CLI
in non-interactive mode.
"""

import os
import shutil
import subprocess
from pathlib import Path

from maestro._vendor import obs
from maestro.models import Task
from maestro.spawners.base import AgentSpawner, spawn_env


# R-07: interim harness-default model (ADR-ECO-002 D1 will supersede this by
# reading the model from routed_agent_type). Pinned to the model the R-07
# sweep benchmarked so the executed model matches the routing decision.
# Override with MAESTRO_CODEX_MODEL.
DEFAULT_CODEX_MODEL = "gpt-5.5"

_obs_log = obs.get_logger("maestro.spawners.codex")


class CodexSpawner(AgentSpawner):
    """Spawner for OpenAI Codex CLI.

    Runs Codex non-interactively via ``codex exec`` with the
    ``workspace-write`` sandbox so it can edit files in the workdir without
    user interaction. The model is pinned to ``DEFAULT_CODEX_MODEL``;
    routed model wins; ``MAESTRO_CODEX_MODEL`` is the fallback when
    routing supplies none.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "codex_cli"

    def is_available(self) -> bool:
        """Check if Codex CLI is installed.

        Returns:
            True if 'codex' command is available in PATH.
        """
        return shutil.which("codex") is not None

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> subprocess.Popen[bytes]:
        """Spawn Codex process.

        Runs ``codex exec`` with the ``workspace-write`` sandbox for
        non-interactive execution. Output is captured to the log file.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            retry_context: Error context from previous failed attempt.
            model: Routed model from the arbiter. Wins over
                ``MAESTRO_CODEX_MODEL`` and ``DEFAULT_CODEX_MODEL``
                (precedence: routed > env > default).

        Returns:
            Subprocess handle for monitoring.
        """
        prompt = self.build_prompt(task, context, retry_context)
        if model:
            resolved, source = model, "routed"
        elif os.environ.get("MAESTRO_CODEX_MODEL"):
            resolved, source = os.environ["MAESTRO_CODEX_MODEL"], "env"
        else:
            resolved, source = DEFAULT_CODEX_MODEL, "default"
        _obs_log.info(
            "agent.model_resolved",
            harness="codex_cli",
            model=resolved,
            source=source,
        )

        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            process = subprocess.Popen(
                [
                    "codex",
                    "exec",
                    "-m",
                    resolved,
                    "--sandbox",
                    "workspace-write",
                    "--skip-git-repo-check",
                    prompt,
                ],
                cwd=workdir,
                env=spawn_env(),
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)

        return process
