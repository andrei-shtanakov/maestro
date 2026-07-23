import os as _os
import stat as _stat
from pathlib import Path

import pytest as _pytest

from maestro.execution.exec_config import DockerConfig
from maestro.execution.isolators import BareIsolator, DockerIsolator
from maestro.execution.models import CollectPolicy, ExecutionRequest, PreparedRun


def _req(**kw) -> ExecutionRequest:
    base = {
        "run_id": "task-1",
        "argv": ["claude", "-p", "hi"],
        "workdir": Path("/tmp/wd"),
        "log_path": Path("/tmp/wd/log"),
        "collect": CollectPolicy(mode="none"),
    }
    base.update(kw)
    return ExecutionRequest(**base)


def _docker_iso(**cfg_kw) -> DockerIsolator:  # type: ignore[misc]
    cfg = DockerConfig(image="maestro-runner:x", **cfg_kw)
    return DockerIsolator(cfg)


def test_bare_inherit_env_merges_host_then_trace():
    iso = BareIsolator()
    plan = iso.prepare(
        _req(inherit_env=True),
        trace_env={"TRACEPARENT": "tp"},
        host_env={"PATH": "/bin", "TRACEPARENT": "old"},
    )
    assert plan.argv == ["claude", "-p", "hi"]
    assert plan.env == {"PATH": "/bin", "TRACEPARENT": "tp"}
    assert plan.container_name is None


def test_bare_allowlist_env_when_not_inheriting():
    iso = BareIsolator()
    plan = iso.prepare(
        _req(secret_env=["ANTHROPIC_API_KEY", "MISSING"], env={"X": "1"}),
        trace_env={"TRACEPARENT": "tp"},
        host_env={"ANTHROPIC_API_KEY": "sk-abc", "PATH": "/bin"},
    )
    # host PATH is NOT inherited; only allowlisted secret + explicit env + trace
    assert plan.env == {
        "ANTHROPIC_API_KEY": "sk-abc",
        "X": "1",
        "TRACEPARENT": "tp",
    }


def test_bare_materialize_is_noop_and_transport_ref_is_local_pid():
    iso = BareIsolator()
    plan = iso.prepare(_req(), trace_env={}, host_env={})
    prepared = iso.materialize(plan)
    assert prepared.env_file is None
    assert prepared.cleanup_paths == []
    assert iso.transport_ref(prepared, 4242) == "local_pid:4242"


def test_docker_prepare_requires_execution_id() -> None:
    with _pytest.raises(ValueError):
        _docker_iso().prepare(_req(), trace_env={}, host_env={})


def test_docker_prepare_builds_run_argv_with_mounts_labels() -> None:
    iso = _docker_iso(network="none", memory="8g", cpus="2", user="1000:1000")
    req = _req(
        execution_id="e-123",
        entity_kind="task",
        attempt=2,
        argv=["claude", "-p", "hi"],
    )
    plan = iso.prepare(req, trace_env={"TRACEPARENT": "tp"}, host_env={})
    assert plan.container_name == "maestro-e-123"
    assert plan.argv[0:2] == ["docker", "run"]
    assert "--name" in plan.argv and "maestro-e-123" in plan.argv
    # workspace bind mount is the only -v; docker socket never mounted
    assert plan.argv.count("-v") == 1
    assert f"{req.workdir}:/work" in plan.argv
    assert "-w" in plan.argv and "/work" in plan.argv
    assert "--network" in plan.argv and "none" in plan.argv
    assert "--memory" in plan.argv and "8g" in plan.argv
    assert "--user" in plan.argv and "1000:1000" in plan.argv
    assert "--rm" not in plan.argv  # execution containers never use --rm
    # identity labels
    joined = " ".join(plan.argv)
    assert "maestro.execution_id=e-123" in joined
    assert "maestro.entity_kind=task" in joined
    assert "maestro.attempt=2" in joined
    # trace env inlined via -e; original argv preserved at the tail
    assert "TRACEPARENT=tp" in joined
    assert plan.argv[-3:] == ["claude", "-p", "hi"]
    # secret names planned but no --env-file yet when there are no secrets
    assert plan.env_file_keys == []


def test_docker_prepare_plans_env_file_for_secrets() -> None:
    iso = _docker_iso(secret_env=["ANTHROPIC_API_KEY"])
    plan = iso.prepare(
        _req(execution_id="e-9"),
        trace_env={},
        host_env={"ANTHROPIC_API_KEY": "sk-secret-key-xyz"},
    )
    assert plan.env_file_keys == ["ANTHROPIC_API_KEY"]
    assert "--env-file" in plan.argv
    # the value never appears in argv
    assert "sk-secret-key-xyz" not in " ".join(plan.argv)


def test_docker_prepare_env_includes_host_env_for_docker_cli() -> None:
    iso = _docker_iso(secret_env=["ANTHROPIC_API_KEY"])
    host_env_input = {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///x",
        "ANTHROPIC_API_KEY": "sk-secret-key-xyz",
    }
    plan = iso.prepare(
        _req(execution_id="e-9"),
        trace_env={},
        host_env=host_env_input,
    )
    # env dict for docker CLI subprocess needs host env
    assert plan.env["PATH"] == "/usr/bin"
    assert plan.env["DOCKER_HOST"] == "unix:///x"
    # secret value appears in env dict (ok for CLI's host env) but not in argv
    assert plan.env["ANTHROPIC_API_KEY"] == "sk-secret-key-xyz"
    assert "sk-secret-key-xyz" not in " ".join(plan.argv)
    # secret is tracked for env-file, not inlined into argv
    assert plan.env_file_keys == ["ANTHROPIC_API_KEY"]


def test_docker_materialize_writes_0600_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    iso = _docker_iso(secret_env=["ANTHROPIC_API_KEY"])
    plan = iso.prepare(
        _req(execution_id="e-mat"), trace_env={}, host_env=dict(_os.environ)
    )
    prepared = iso.materialize(plan)
    assert prepared.env_file is not None and prepared.env_file.exists()
    mode = _stat.S_IMODE(prepared.env_file.stat().st_mode)
    assert mode == 0o600
    dir_mode = _stat.S_IMODE(prepared.env_file.parent.stat().st_mode)
    assert dir_mode == 0o700
    assert "ANTHROPIC_API_KEY=sk-secret" in prepared.env_file.read_text()
    assert prepared.env_file in prepared.cleanup_paths
    assert plan.tmp_dir in prepared.cleanup_paths


def test_docker_materialize_rejects_newline_in_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("BAD", "line1\nline2")
    iso = _docker_iso(secret_env=["BAD"])
    plan = iso.prepare(
        _req(execution_id="e-bad"), trace_env={}, host_env=dict(_os.environ)
    )
    with _pytest.raises(ValueError):
        iso.materialize(plan)
    # self-clean on failure (carry-forward from Task 3 review): no partial dir left
    assert plan.tmp_dir is not None
    assert not plan.tmp_dir.exists()


def test_docker_transport_ref_is_docker_container_name() -> None:
    iso = _docker_iso()
    plan = iso.prepare(_req(execution_id="e-ref"), trace_env={}, host_env={})
    # transport_ref only reads prepared.plan.container_name; build PreparedRun
    # directly rather than calling materialize() so the test stays hermetic and
    # never touches the filesystem (materialize would create a real tmp dir).
    prepared = PreparedRun(plan=plan)
    assert iso.transport_ref(prepared, 999) == "docker:maestro-e-ref"
