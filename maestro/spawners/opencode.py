"""opencode spawner implementation.

Runs the opencode agentic CLI (``opencode run``) non-interactively over the
workdir. The model is resolved from the catalog; routed model wins, then
``MAESTRO_OPENCODE_MODEL``, then the catalog default.

First routable *open* harness: open models (glm-5.1, qwen3.6, …) reach routing
as the model under this harness (``opencode@glm-5.1``), rather than each chat
endpoint getting its own spawner. Chat-endpoint harnesses (mimo, qwen, deepseek)
have no file-editing agency and are never routable — see ADR-ECO-003c.
"""

from pathlib import Path

from maestro._vendor import obs
from maestro.catalog import load_catalog, resolve_model, warn_on_model_status
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import Task
from maestro.spawners.base import AgentSpawner


_obs_log = obs.get_logger("maestro.spawners.opencode")


def _qualify(model: str) -> str:
    """Bare model id gets opencode's provider prefix; an already
    provider-qualified id (contains '/') passes through unchanged.

    Mirrors ATP's ``method/spawners/_cli_common.py:model_arg`` so a catalog id
    like ``glm-5.1`` becomes ``opencode/glm-5.1`` for the CLI.
    """
    return model if "/" in model else f"opencode/{model}"


class OpencodeSpawner(AgentSpawner):
    """Spawner for the opencode agentic CLI (open-model harness).

    Runs ``opencode run --format json`` non-interactively. The model is
    resolved from the catalog; routed model wins, then
    ``MAESTRO_OPENCODE_MODEL``, then the catalog default.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "opencode"

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
        """Build a transport-agnostic ExecutionRequest for opencode.

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
                ``MAESTRO_OPENCODE_MODEL`` and the catalog default
                (precedence: routed > env > catalog).

        Returns:
            Transport-agnostic execution request.
        """
        prompt = self.build_prompt(task, context, retry_context)
        catalog = load_catalog()
        resolved, source = resolve_model(
            model, "MAESTRO_OPENCODE_MODEL", "opencode", catalog
        )
        _obs_log.info(
            "agent.model_resolved",
            harness="opencode",
            model=resolved,
            source=source,
        )
        warn_on_model_status(resolved, source, catalog)
        return ExecutionRequest(
            run_id=run_id,
            argv=[
                "opencode",
                "run",
                "--format",
                "json",
                "-m",
                _qualify(resolved),
                prompt,
            ],
            workdir=workdir,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
            required_tools=["opencode"],
        )
