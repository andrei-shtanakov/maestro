import pytest

from maestro.execution.exec_config import ExecutionConfig
from maestro.execution.local import LocalBackend
from maestro.execution.resolver import BackendResolver, ExecutionConfigError


def test_no_execution_config_resolves_local():
    r = BackendResolver(None)
    assert r.default_name == "local"
    assert isinstance(r.resolve(None), LocalBackend)
    assert isinstance(r.resolve("local"), LocalBackend)


def test_unknown_backend_name_fails_fast():
    r = BackendResolver(ExecutionConfig())
    with pytest.raises(ExecutionConfigError):
        r.resolve("gpu-box")


def test_docker_without_docker_config_fails_fast():
    r = BackendResolver(ExecutionConfig(default_backend="local"))
    with pytest.raises(ExecutionConfigError):
        r.resolve("docker")


def test_local_instance_is_cached():
    r = BackendResolver(None)
    assert r.resolve("local") is r.resolve("local")
