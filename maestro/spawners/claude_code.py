"""Claude Code spawner implementation.

This module provides the ClaudeCodeSpawner for running Claude Code
in headless mode with JSON output format.
"""

import os
import shutil
import subprocess
from pathlib import Path

from maestro._vendor import obs
from maestro.models import Task
from maestro.spawners.base import AgentSpawner, resolve_model, spawn_env


# R-07: interim harness-default model (ADR-ECO-002 D1 will supersede this by
# reading the model from routed_agent_type). Pinned to the model the R-07
# sweep benchmarked so the executed model matches the routing decision.
# Fallback via MAESTRO_CLAUDE_MODEL when routing supplies no model.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

_obs_log = obs.get_logger("maestro.spawners.claude_code")


class ClaudeCodeSpawner(AgentSpawner):
    """Spawner for Claude Code in headless mode.

    Runs Claude Code with --print and --output-format json flags
    for non-interactive execution. The model is pinned to
    ``DEFAULT_CLAUDE_MODEL``; routed model wins; ``MAESTRO_CLAUDE_MODEL``
    is the fallback when routing supplies none.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "claude_code"

    def is_available(self) -> bool:
        """Check if Claude Code CLI is installed.

        Returns:
            True if 'claude' command is available in PATH.
        """
        return shutil.which("claude") is not None

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
        """Spawn Claude Code process.

        Runs Claude Code in headless mode with JSON output.
        The process output (stdout and stderr) is captured to the log file.

        Note: This method opens the log file and passes a duplicated file
        descriptor to the subprocess. The original file handle is closed
        immediately to avoid resource warnings.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            retry_context: Error context from previous failed attempt.
            model: Routed model from the arbiter. Wins over
                ``MAESTRO_CLAUDE_MODEL`` and ``DEFAULT_CLAUDE_MODEL``
                (precedence: routed > env > default).

        Returns:
            Subprocess handle for monitoring.
        """
        prompt = self.build_prompt(task, context, retry_context)
        resolved, source = resolve_model(
            model, "MAESTRO_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL
        )
        _obs_log.info(
            "agent.model_resolved",
            harness="claude_code",
            model=resolved,
            source=source,
        )

        # Open log file and duplicate the fd for subprocess
        # This allows us to close the Python file object without affecting
        # the subprocess's access to the file
        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            process = subprocess.Popen(
                [
                    "claude",
                    "--model",
                    resolved,
                    "--print",
                    "--output-format",
                    "json",
                    "-p",
                    prompt,
                ],
                cwd=workdir,
                env=spawn_env(),
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)  # Close our copy, subprocess has its own

        return process
