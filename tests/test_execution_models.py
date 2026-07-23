from datetime import UTC, datetime
from pathlib import Path

from maestro.execution.models import (
    CollectPolicy,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
)


def test_execution_request_minimal_defaults():
    req = ExecutionRequest(
        run_id="r1",
        argv=["echo", "hi"],
        workdir=Path("/tmp/wd"),
        log_path=Path("/tmp/wd/out.log"),
        collect=CollectPolicy(mode="none"),
    )
    assert req.env == {}
    assert req.secret_env == []
    assert req.inherit_env is False
    assert req.capture_output is False
    assert req.progress_mirror is None
    assert req.labels == {}
    assert req.required_tools == []
    # mutable defaults are per-instance, not shared
    req.env["A"] = "1"
    other = ExecutionRequest(
        run_id="r2",
        argv=["true"],
        workdir=Path("/tmp"),
        log_path=Path("/tmp/o.log"),
        collect=CollectPolicy(mode="none"),
    )
    assert other.env == {}


def test_collect_policy_defaults_and_modes():
    p = CollectPolicy(mode="scope_paths")
    assert p.exclude == [".git/**", ".maestro/**"]
    assert p.conflict_policy == "fail"
    assert p.on_failure == "collect"


def test_execution_result_capture_fields():
    r = ExecutionResult(exit_code=0, output_log_path=Path("/tmp/o.log"))
    assert r.stdout_tail == ""
    assert r.stderr_tail == ""
    assert r.timed_out is False
    assert r.error_message is None


def test_handle_ref_roundtrip():
    ref = ExecutionHandleRef(
        backend_id="local",
        run_id="r1",
        transport_ref="local_pid:123",
        started_at=datetime.now(UTC),
    )
    assert ref.status_marker is None
    assert ref.workdir_mirror_path is None


def test_protocols_importable():
    from maestro.execution.backend import ExecutionBackend, TaskHandle

    # Protocols are runtime-checkable enough to reference; just assert identity.
    assert TaskHandle.__name__ == "TaskHandle"
    assert ExecutionBackend.__name__ == "ExecutionBackend"


def _req(**kw) -> ExecutionRequest:
    base = {
        "run_id": "task-1",
        "argv": ["echo", "hi"],
        "workdir": Path("/tmp/wd"),
        "log_path": Path("/tmp/wd/log"),
        "collect": CollectPolicy(mode="none"),
    }
    base.update(kw)
    return ExecutionRequest(**base)


def test_launch_fields_default_to_local_compatible_values():
    req = _req()
    assert req.execution_id is None
    assert req.entity_kind is None
    assert req.attempt == 1
    assert req.backend_id == "local"


def test_launch_fields_round_trip():
    req = _req(
        execution_id="11111111-1111-4111-8111-111111111111",
        entity_kind="workstream",
        attempt=3,
        backend_id="docker",
    )
    again = ExecutionRequest.model_validate(req.model_dump())
    assert again.entity_kind == "workstream"
    assert again.attempt == 3
    assert again.backend_id == "docker"


def test_prepared_run_plan_defaults():
    from maestro.execution.models import PreparedRun, PreparedRunPlan

    plan = PreparedRunPlan(argv=["docker", "run"], env={"A": "1"})
    assert plan.container_name is None
    assert plan.labels == {}
    assert plan.env_file_keys == []
    assert plan.cidfile_path is None
    assert plan.tmp_dir is None
    prepared = PreparedRun(plan=plan)
    assert prepared.env_file is None
    assert prepared.cleanup_paths == []
