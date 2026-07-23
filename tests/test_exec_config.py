"""Tests for the `execution:` config surface (DockerConfig/ExecutionConfig)."""

import pytest
from pydantic import ValidationError

from maestro.execution.exec_config import DockerConfig, ExecutionConfig
from maestro.models import (
    ProjectConfig,
    Task,
    TaskConfig,
    Workstream,
    WorkstreamConfig,
)


def test_defaults_local_no_docker():
    cfg = ExecutionConfig()
    assert cfg.default_backend == "local"
    assert cfg.docker is None


def test_docker_config_network_defaults_none():
    d = DockerConfig(image="maestro-runner:x")
    assert d.network == "none"
    assert d.secret_env == []


def test_gh_denylist_rejected_in_secret_env():
    with pytest.raises(ValidationError):
        DockerConfig(image="i", secret_env=["ANTHROPIC_API_KEY", "GH_TOKEN"])
    with pytest.raises(ValidationError):
        DockerConfig(image="i", secret_env=["GH_FOO"])


def test_project_config_execution_round_trip():
    cfg = ProjectConfig.model_validate(
        {
            "project": "demo",
            "repo": "/tmp/demo",
            "tasks": [{"id": "t", "title": "T", "prompt": "p", "backend": "docker"}],
            "execution": {
                "default_backend": "local",
                "docker": {
                    "image": "maestro-runner:x",
                    "secret_env": ["ANTHROPIC_API_KEY"],
                },
            },
        }
    )
    assert cfg.execution is not None
    assert cfg.execution.docker is not None
    assert cfg.execution.docker.image == "maestro-runner:x"
    assert cfg.tasks[0].backend == "docker"


def test_task_from_config_threads_backend_through():
    config = TaskConfig(id="t", title="T", prompt="p", backend="docker")
    task = Task.from_config(config, workdir="/tmp/demo")
    assert task.backend == "docker"


def test_task_from_config_defaults_backend_to_none():
    config = TaskConfig(id="t", title="T", prompt="p")
    task = Task.from_config(config, workdir="/tmp/demo")
    assert task.backend is None


def test_workstream_from_config_threads_backend_through():
    config = WorkstreamConfig(id="w", title="W", description="d", backend="docker")
    workstream = Workstream.from_config(config)
    assert workstream.backend == "docker"


def test_workstream_from_config_defaults_backend_to_none():
    config = WorkstreamConfig(id="w", title="W", description="d")
    workstream = Workstream.from_config(config)
    assert workstream.backend is None
