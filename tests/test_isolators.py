from pathlib import Path

from maestro.execution.isolators import BareIsolator
from maestro.execution.models import CollectPolicy, ExecutionRequest


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
