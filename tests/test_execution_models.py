from datetime import UTC, datetime
from pathlib import Path

from maestro.execution.models import (
    CollectPolicy,
    ExecutionRequest,
    ExecutionResult,
    ExecutionHandleRef,
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
