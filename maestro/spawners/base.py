"""Base class for agent spawners.

This module defines the abstract base class for all agent spawners in Maestro.
New agent types can be added by subclassing AgentSpawner and implementing
the required methods.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from maestro.execution.models import ExecutionRequest
from maestro.models import Task


class AgentSpawner(ABC):
    """Abstract base class for agent spawners.

    All agent spawners must inherit from this class and implement
    the required abstract methods. The spawner is responsible for:
    - Building prompts with task details and context
    - Building a transport-agnostic ExecutionRequest for the run
    """

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Unique identifier for this agent type.

        Returns:
            String identifier matching one of AgentType enum values.
        """
        ...

    @abstractmethod
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
        """Build a transport-agnostic ExecutionRequest ('what to run').

        The backend (LocalBackend/SshBackend) owns spawning ('where/how').
        """
        ...

    def can_build_request(self) -> bool:
        """Whether this spawner can build a valid request locally.

        Default True. Override for spawners with local config prerequisites.
        This is NOT a tool-availability check — that is the backend's job
        (`ExecutionBackend.can_run` probes required_tools on the executor).
        """
        return True

    def build_prompt(
        self,
        task: Task,
        context: str,
        retry_context: str = "",
    ) -> str:
        """Build prompt with task details, dependency context, and retry info.

        This method can be overridden by subclasses to customize
        prompt formatting for specific agents.

        Args:
            task: Task to build prompt for.
            context: Context from completed dependencies.
            retry_context: Error context from previous failed attempt.

        Returns:
            Formatted prompt string.
        """
        scope_str = ", ".join(task.scope) if task.scope else "any"

        prompt = f"""Task: {task.title}

{task.prompt}

Context from completed dependencies:
{context if context else "No prior context available."}

Scope (files you can modify):
{scope_str}
"""
        if retry_context:
            prompt += f"\n{retry_context}"

        return prompt
