"""Execution-backend config models (the narrow Phase-1 `execution` block)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


def is_denylisted(name: str) -> bool:
    """True if `name` is a GitHub credential env var (exact or GH_* prefix)."""
    return name in {"GH_TOKEN", "GITHUB_TOKEN"} or name.startswith("GH_")


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
        bad = [n for n in value if is_denylisted(n)]
        if bad:
            msg = f"secret_env may not carry GitHub credentials: {bad}"
            raise ValueError(msg)
        return value


class ExecutionConfig(BaseModel):
    """The `execution:` block; absent → local + bare, old behavior."""

    default_backend: str = "local"
    docker: DockerConfig | None = None


class LocalTransport(BaseModel):
    """Run the harness in-process on the local machine."""

    type: Literal["local"] = "local"


class SshTransport(BaseModel):
    """Run the harness on a remote host reached over SSH."""

    type: Literal["ssh"]
    host: str
    user: str | None = None
    port: int | None = None
    workdir_root: str
    connect_timeout_s: int = 10
    ssh_opts: list[str] = Field(default_factory=list)

    @field_validator("host")
    @classmethod
    def _reject_composite_host(cls, value: str) -> str:
        """Require a bare hostname/alias; user/port must use their own fields."""
        if "@" in value or ":" in value:
            msg = f"host must be a bare hostname/alias; use user/port fields: {value!r}"
            raise ValueError(msg)
        if not value.strip():
            raise ValueError("host must not be empty")
        return value


class BareIsolation(BaseModel):
    """No isolation; the harness runs directly on the transport's target."""

    type: Literal["bare"] = "bare"


class DockerIsolation(BaseModel):
    """Isolate the harness inside a Docker container."""

    type: Literal["docker"]
    image: str
    network: str = "none"
    memory: str | None = None
    cpus: str | None = None
    user: str | None = None


class BackendSpec(BaseModel):
    """A named execution backend: how to reach it, and how to isolate it."""

    transport: LocalTransport | SshTransport = Field(discriminator="type")
    isolation: BareIsolation | DockerIsolation = Field(discriminator="type")
    secret_env: list[str] = Field(default_factory=list)
    inherit_secret_defaults: bool = False
    max_concurrent: int | None = None

    @field_validator("secret_env")
    @classmethod
    def _reject_gh(cls, value: list[str]) -> list[str]:
        """Reject GitHub credential names so they never reach a backend."""
        bad = [n for n in value if is_denylisted(n)]
        if bad:
            msg = f"secret_env may not carry GitHub credentials: {bad}"
            raise ValueError(msg)
        return value
