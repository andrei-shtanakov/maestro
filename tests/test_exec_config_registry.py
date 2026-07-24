"""Tests for the transport/isolation/backend registry config models."""

import pytest
from pydantic import ValidationError

from maestro.execution.exec_config import (
    BackendSpec,
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
