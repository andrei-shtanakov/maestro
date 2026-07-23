import pytest

from maestro.execution.exec_config import DockerConfig, ExecutionConfig
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


def test_resolve_uses_entity_backend_then_default():
    """Documents the dispatch contract both loops rely on: an entity's own
    `backend` wins when set, otherwise the configured default is used.
    """
    r = BackendResolver(ExecutionConfig(default_backend="local"))
    # entity backend None -> default_name
    assert r.resolve(None).id == "local"
    # explicit local
    assert r.resolve("local").id == "local"


def test_docker_resolves_when_config_present():
    r = BackendResolver(ExecutionConfig(docker=DockerConfig(image="maestro-runner:x")))
    backend = r.resolve("docker")
    assert backend.id == "docker"


@pytest.mark.anyio
async def test_docker_healthcheck_rejects_ssh_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://gpu-box")
    r = BackendResolver(ExecutionConfig(docker=DockerConfig(image="i")))
    backend = r.resolve("docker")
    health = await backend.healthcheck()
    assert health.reachable is False
    assert "DOCKER_HOST" in health.detail
