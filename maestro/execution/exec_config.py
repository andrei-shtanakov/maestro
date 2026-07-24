"""Execution-backend config models (the narrow Phase-1 `execution` block)."""

from pydantic import BaseModel, Field, field_validator


_GH_DENYLIST_EXACT = {"GH_TOKEN", "GITHUB_TOKEN"}


def _is_denylisted(name: str) -> bool:
    """Return True if `name` is a GitHub credential env var (exact or GH_* prefix)."""
    return name in _GH_DENYLIST_EXACT or name.startswith("GH_")


class DockerConfig(BaseModel):
    """Local Docker isolator configuration."""

    image: str
    network: str = "none"
    memory: str | None = None
    cpus: str | None = None
    user: str | None = None
    secret_env: list[str] = Field(default_factory=list)

    @field_validator("secret_env")
    @classmethod
    def _reject_gh(cls, value: list[str]) -> list[str]:
        """Reject GitHub credential names so they never reach a container."""
        bad = [n for n in value if _is_denylisted(n)]
        if bad:
            msg = f"secret_env may not carry GitHub credentials: {bad}"
            raise ValueError(msg)
        return value


class ExecutionConfig(BaseModel):
    """The `execution:` block; absent → local + bare, old behavior."""

    default_backend: str = "local"
    docker: DockerConfig | None = None
