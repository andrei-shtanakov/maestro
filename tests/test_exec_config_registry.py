"""Tests for the transport/isolation/backend registry config models."""

import pytest
from pydantic import ValidationError

from maestro.execution.exec_config import (
    BackendSpec,
    DockerConfig,
    ExecutionConfig,
    SshTransport,
)


def test_ssh_transport_rejects_host_with_user_or_port_token():
    with pytest.raises(ValidationError):
        SshTransport(type="ssh", host="alice@gpu-box", workdir_root="/var/tmp/m")
    with pytest.raises(ValidationError):
        SshTransport(type="ssh", host="gpu-box:22", workdir_root="/var/tmp/m")


def test_ssh_transport_accepts_bare_host_with_separate_user_port():
    t = SshTransport(
        type="ssh", host="gpu-box", user="alice", port=2222, workdir_root="/var/tmp/m"
    )
    assert (t.host, t.user, t.port) == ("gpu-box", "alice", 2222)


def test_backend_spec_secret_env_rejects_github_creds():
    with pytest.raises(ValidationError):
        BackendSpec(
            transport=SshTransport(type="ssh", host="h", workdir_root="/w"),
            isolation={"type": "bare"},
            secret_env=["ANTHROPIC_API_KEY", "GH_TOKEN"],
        )


def test_local_is_implicit_backend():
    cfg = ExecutionConfig()
    reg = cfg.normalized()
    assert "local" in reg and reg["local"].transport.type == "local"


def test_legacy_docker_is_shimmed_into_registry():
    cfg = ExecutionConfig(
        docker=DockerConfig(image="img:1", secret_env=["ANTHROPIC_API_KEY"])
    )
    reg = cfg.normalized()
    assert reg["docker"].isolation.type == "docker"
    assert reg["docker"].isolation.image == "img:1"
    assert reg["docker"].secret_env == ["ANTHROPIC_API_KEY"]
    assert reg["docker"].transport.type == "local"


def test_legacy_and_canonical_docker_collision_raises():
    cfg = ExecutionConfig(
        docker=DockerConfig(image="img:1"),
        backends={
            "docker": BackendSpec(
                transport={"type": "local"},
                isolation={"type": "docker", "image": "img:2"},
            )
        },
    )
    with pytest.raises(ValueError, match="docker"):
        cfg.normalized()


def test_effective_secret_env_inherits_defaults_only_when_flagged():
    cfg = ExecutionConfig(
        secret_env_defaults=["ANTHROPIC_API_KEY"],
        backends={
            "a": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/w"),
                isolation={"type": "bare"},
                secret_env=["OPENAI_API_KEY"],
                inherit_secret_defaults=True,
            ),
            "b": BackendSpec(
                transport=SshTransport(type="ssh", host="h2", workdir_root="/w"),
                isolation={"type": "bare"},
                secret_env=["OPENAI_API_KEY"],
            ),
        },
    )
    assert set(cfg.effective_secret_env("a")) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    assert cfg.effective_secret_env("b") == ["OPENAI_API_KEY"]
