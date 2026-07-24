import pytest

from maestro.execution.exec_config import BackendSpec, ExecutionConfig, SshTransport
from maestro.execution.resolver import BackendResolver, ExecutionConfigError


def _ssh_cfg() -> ExecutionConfig:
    return ExecutionConfig(
        default_backend="local",
        backends={
            "gpu": BackendSpec(
                transport=SshTransport(type="ssh", host="gpu", workdir_root="/w"),
                isolation={"type": "bare"},
            )
        },
    )


def test_local_resolves_with_no_config():
    r = BackendResolver(None)
    assert r.resolve(None).id == "local"


def test_unknown_backend_raises():
    with pytest.raises(ExecutionConfigError, match="unknown"):
        BackendResolver(_ssh_cfg()).resolve("nope")


def test_ssh_backend_rejected_in_scheduler_mode():
    r = BackendResolver(_ssh_cfg(), mode="scheduler")
    with pytest.raises(ExecutionConfigError, match="Mode-2"):
        r.resolve("gpu")
