"""Per-dispatch backend resolution. Fail-fast; never falls back to local."""

from maestro.execution.backend import ExecutionBackend
from maestro.execution.exec_config import ExecutionConfig
from maestro.execution.local import LocalBackend


class ExecutionConfigError(Exception):
    """Raised for an unusable execution config (unknown/mis-specified backend)."""


class BackendResolver:
    """Resolves a backend name to an ExecutionBackend, caching instances."""

    def __init__(self, execution: ExecutionConfig | None) -> None:
        self._execution = execution or ExecutionConfig()
        self._cache: dict[str, ExecutionBackend] = {}

    @property
    def default_name(self) -> str:
        return self._execution.default_backend

    def resolve(self, name: str | None) -> ExecutionBackend:
        backend_name = name or self._execution.default_backend
        if backend_name in self._cache:
            return self._cache[backend_name]
        backend = self._build(backend_name)
        self._cache[backend_name] = backend
        return backend

    def _build(self, name: str) -> ExecutionBackend:
        if name == "local":
            return LocalBackend()
        if name == "docker":
            if self._execution.docker is None:
                raise ExecutionConfigError(
                    "backend 'docker' selected but no execution.docker config"
                )
            # Real docker backend is wired in Task 13.
            raise ExecutionConfigError("docker backend not yet available (Task 13)")
        raise ExecutionConfigError(f"unknown backend '{name}'")
