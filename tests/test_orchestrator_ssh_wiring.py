"""Task 16 (F2): orchestrator SSH request wiring.

Unit-level: the pure `build_ssh_execution_request` helper (Step 1 of the
task brief). Wiring-level: `_spawn_workstream`'s ssh branch, the
`_update_progress` mirror-dir read, `_monitor_running`'s phased-finalize
callbacks + collect-failure routing, and the `_probe_open_handle` /
`_gc_terminal_handles` ssh recovery branches — all exercised against a real
(sqlite) `Database`, mirroring the patterns in tests/test_orchestrator.py's
`TestStartupRecovery`.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.database import Database
from maestro.execution.exec_config import SshTransport
from maestro.execution.models import (
    BackendHealth,
    CollectPolicy,
    ExecutionRequest,
)
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_launch import encode_transport_ref, remote_layout
from maestro.models import OrchestratorConfig, Workstream, WorkstreamStatus
from maestro.orchestrator import Orchestrator, RunningWorkstream
from tests.fakes.fake_execution_backend import FakeTaskHandle


@pytest.fixture(autouse=True)
def _no_real_git_excludes():
    """Default-patch `ensure_harness_excludes` (git-backed) to a no-op.

    Mirrors test_orchestrator.py's fixture of the same name: these tests
    exercise `_spawn_workstream` against a plain tmp_path workspace, not a
    real git repo — without this, the real H-7 call raises `WorkspaceError`
    before `generate_spec` ever runs.
    """
    from unittest.mock import patch

    with patch("maestro.orchestrator.ensure_harness_excludes"):
        yield


# =============================================================================
# Step 1: pure `build_ssh_execution_request` helper
# =============================================================================


def test_ssh_request_uses_whole_worktree_collect_and_mirror():
    # Unit-level: the helper that builds the request for a non-local backend.
    from maestro.orchestrator import build_ssh_execution_request  # new pure helper

    req = build_ssh_execution_request(
        workstream_id="api",
        workspace="/tmp/wt",
        log_file="/tmp/api.log",
        cmd=["spec-runner", "run", "--all"],
        execution_id="e1",
        attempt=1,
        mirror_dir="/tmp/mirror",
    )
    assert isinstance(req, ExecutionRequest)
    assert req.collect.mode == "whole_worktree"
    assert req.collect.conflict_policy == "fail"
    assert req.progress_mirror is not None
    assert str(req.progress_mirror.local_dir) == "/tmp/mirror"
    assert req.required_tools == ["spec-runner"]


def test_ssh_request_fields_match_contract():
    """CollectPolicy.on_failure + inherit_env + entity_kind, per the brief."""
    from maestro.orchestrator import build_ssh_execution_request

    req = build_ssh_execution_request(
        workstream_id="api",
        workspace="/tmp/wt",
        log_file="/tmp/api.log",
        cmd=["spec-runner", "run", "--all"],
        execution_id="e1",
        attempt=2,
        mirror_dir="/tmp/mirror",
    )
    assert req.collect.on_failure == "collect"
    assert req.inherit_env is False
    assert req.entity_kind == "workstream"
    assert req.attempt == 2
    assert req.execution_id == "e1"
    assert req.run_id == "api"
    mirror = req.progress_mirror
    assert mirror is not None
    assert mirror.kind == "spec_runner_sqlite"
    assert mirror.remote_globs == []
    assert mirror.interval_seconds == 2.0


def test_collect_policy_import_sanity():
    """CollectPolicy stays importable from maestro.execution.models (used
    directly by both the local/docker and the ssh request builders)."""
    p = CollectPolicy(mode="none")
    assert p.mode == "none"


# =============================================================================
# Shared helpers
# =============================================================================


def _ssh_backend(host: str = "gpu") -> SshBackend:
    """A real SshBackend over a no-op fake Runner (no subprocess/network)."""

    async def runner(argv, stdin):
        del argv, stdin
        return RunResult(0, "", "")

    transport = SshTransport(type="ssh", host=host, workdir_root="/var/tmp/m")
    return SshBackend(host, transport, secret_env=[], runner=runner)


async def _orch_with_db(tmp_path: Path) -> tuple[Orchestrator, Database]:
    db = Database(tmp_path / "o.db")
    await db.connect()
    config = OrchestratorConfig(
        project="test-project",
        repo_url="https://github.com/test/repo",
        repo_path=str(tmp_path / "repo"),
        workspace_base=str(tmp_path / "ws"),
        max_concurrent=2,
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=MagicMock(),
        decomposer=MagicMock(),
        pr_manager=MagicMock(),
        config=config,
        log_dir=tmp_path / "logs",
    )
    return orch, db


def _seed(zid: str, status: WorkstreamStatus, *, backend: str | None = None):
    return Workstream(
        id=zid,
        title=zid,
        description="d",
        scope=["s"],
        branch=f"feature/{zid}",
        status=status,
        backend=backend,
    )


class _RaisingCollectHandle(FakeTaskHandle):
    """FakeTaskHandle whose `.collect()` raises — drives the finalize
    collect-failure branch without needing a real ssh transfer."""

    async def collect(self):
        raise RuntimeError("collect conflict: remote diff clashes with local HEAD")


# =============================================================================
# Step 3: `_spawn_workstream` ssh branch
# =============================================================================


class TestSpawnWorkstreamSshBranch:
    @pytest.mark.anyio
    async def test_ssh_backend_builds_whole_worktree_request_and_mirror_dir(
        self, tmp_path
    ) -> None:
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            recorded: dict[str, ExecutionRequest] = {}

            async def fake_run(req: ExecutionRequest) -> FakeTaskHandle:
                recorded["req"] = req
                return FakeTaskHandle(exit_code=0, pid=777)

            async def fake_healthcheck() -> BackendHealth:
                return BackendHealth(reachable=True)

            backend.run = fake_run  # type: ignore[method-assign]
            backend.healthcheck = fake_healthcheck  # type: ignore[method-assign]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            workspace = tmp_path / "ws-w"
            workspace.mkdir()
            cast_mgr = orch._workspace_mgr
            cast_mgr.workspace_exists = MagicMock(return_value=True)
            cast_mgr.get_workspace_path = MagicMock(return_value=workspace)
            orch._decomposer.generate_spec = AsyncMock()

            await orch._spawn_workstream("w")

            req = recorded["req"]
            assert req.collect.mode == "whole_worktree"
            assert req.collect.conflict_policy == "fail"
            assert req.progress_mirror is not None
            assert req.backend_id == "gpu"
            assert req.execution_id is not None

            running = orch._running["w"]
            assert running.mirror_dir == tmp_path / "logs" / "w.mirror"
            assert str(req.progress_mirror.local_dir) == str(running.mirror_dir)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_local_backend_request_unchanged(self, tmp_path) -> None:
        """CRITICAL — zero regression: the local branch still builds the
        old `CollectPolicy(mode="none")` request with no progress_mirror,
        and `mirror_dir` stays None on RunningWorkstream."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            recorded: dict[str, ExecutionRequest] = {}

            class _LocalFake:
                id = "local"

                async def healthcheck(self) -> BackendHealth:
                    return BackendHealth(reachable=True)

                async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
                    recorded["req"] = req
                    return FakeTaskHandle(exit_code=0, pid=1)

            orch._backends.resolve = MagicMock(return_value=_LocalFake())

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            workspace = tmp_path / "ws-w"
            workspace.mkdir()
            orch._workspace_mgr.workspace_exists = MagicMock(return_value=True)
            orch._workspace_mgr.get_workspace_path = MagicMock(return_value=workspace)
            orch._decomposer.generate_spec = AsyncMock()

            await orch._spawn_workstream("w")

            req = recorded["req"]
            assert req.collect.mode == "none"
            assert req.progress_mirror is None
            assert orch._running["w"].mirror_dir is None
        finally:
            await db.close()


# =============================================================================
# Step 4: `_update_progress` reads from the mirror dir when set
# =============================================================================


class TestUpdateProgressMirror:
    @pytest.mark.anyio
    async def test_reads_mirror_dir_when_set(self, tmp_path, monkeypatch) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.RUNNING))
            seen: dict[str, Path] = {}

            def fake_read_state(spec_dir, prefix):
                seen["spec_dir"] = spec_dir
                return None

            monkeypatch.setattr(orch_mod, "read_executor_state", fake_read_state)

            mirror = tmp_path / "logs" / "w.mirror"
            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                mirror_dir=mirror,
            )
            await orch._update_progress("w", running)
            assert seen["spec_dir"] == mirror
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_reads_workspace_spec_dir_when_mirror_unset(
        self, tmp_path, monkeypatch
    ) -> None:
        """Unchanged local/docker behavior: no mirror -> live workspace spec/."""
        from maestro import orchestrator as orch_mod

        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.RUNNING))
            seen: dict[str, Path] = {}

            def fake_read_state(spec_dir, prefix):
                seen["spec_dir"] = spec_dir
                return None

            monkeypatch.setattr(orch_mod, "read_executor_state", fake_read_state)

            workspace = tmp_path / "ws-w"
            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(),
                started_at=datetime.now(UTC),
                workspace_path=workspace,
                log_file=tmp_path / "logs" / "w.log",
            )
            await orch._update_progress("w", running)
            assert seen["spec_dir"] == workspace / "spec"
        finally:
            await db.close()


# =============================================================================
# Step 5: `_monitor_running` phased finalize + collect-failure routing
# =============================================================================


class TestMonitorRunningFinalize:
    @pytest.mark.anyio
    async def test_collect_failure_routes_to_needs_review(self, tmp_path) -> None:
        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref="gpu:maestro-e1",
                attempt=1,
            )
            called: list[str] = []
            orch._handle_completion = AsyncMock(
                side_effect=lambda *_a, **_k: called.append("completion")
            )

            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=_RaisingCollectHandle(exit_code=0, pid=42),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                execution_id="e1",
                backend_id="gpu",
            )
            orch._running["w"] = running

            await orch._monitor_running()

            assert called == []  # _handle_completion never reached
            assert "w" not in orch._running
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert "collect" in (w.error_message or "").lower()

            handles = await db.get_open_execution_handles()
            row = next(h for h in handles if h["execution_id"] == "e1")
            # Terminal persisted (on_terminal fired); never reached collected.
            assert row["state"] == "terminal"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_collect_success_proceeds_to_handle_completion(
        self, tmp_path
    ) -> None:
        """Docker/local: collect() no-ops and always succeeds, so this path
        is functionally unchanged from before Task 16 — only the
        intermediate DB phases (terminal -> collected -> cleaned) now
        persist between wait/collect/cleanup instead of all at once."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="docker",
                transport_ref="docker:maestro-e1",
                attempt=1,
            )
            calls: list[tuple] = []
            orch._handle_completion = AsyncMock(
                side_effect=lambda *a, **_k: calls.append(a)
            )

            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(exit_code=0, pid=42),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                execution_id="e1",
                backend_id="docker",
            )
            orch._running["w"] = running

            await orch._monitor_running()

            assert len(calls) == 1
            assert calls[0][0] == "w"
            assert calls[0][2] == 0  # exit code
            assert "w" not in orch._running

            handles = await db.get_open_execution_handles()
            # cleaned handles are excluded from get_open_execution_handles
            assert all(h["execution_id"] != "e1" for h in handles)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_local_handle_with_no_execution_id_is_a_noop_for_db(
        self, tmp_path
    ) -> None:
        """No execution_id (the plain local path) -> the on_terminal/
        on_collected callbacks are no-ops; no DB handle bookkeeping."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            orch._handle_completion = AsyncMock()
            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(exit_code=0, pid=1),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                execution_id=None,
                backend_id="local",
            )
            orch._running["w"] = running

            await orch._monitor_running()

            orch._handle_completion.assert_awaited_once()
            assert "w" not in orch._running
        finally:
            await db.close()


# =============================================================================
# Step 6: ssh recovery branch (`_probe_open_handle` / `_gc_terminal_handles`)
# =============================================================================


class TestSshRecoveryBranch:
    @pytest.mark.anyio
    async def test_probe_open_handle_uses_probe_ssh_directly_not_backend_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()

            async def _refuse_probe(ref):
                raise AssertionError(
                    "backend.probe() must not be called; probe_ssh directly"
                )

            backend.probe = _refuse_probe  # type: ignore[method-assign]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            w = await db.get_workstream("w")
            # probe_ssh is fail-closed: always needs_review=True.
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_terminal_handles_sweeps_collected_ssh_row(self, tmp_path) -> None:
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            rm_calls: list[list[str]] = []

            async def runner(argv, stdin):
                del stdin
                joined = " ".join(argv)
                if ".maestro-owner" in joined:
                    return RunResult(0, "w\n", "")
                if joined.startswith("ssh") and "rm " in joined:
                    rm_calls.append(argv)
                    return RunResult(0, "", "")
                return RunResult(0, "", "")

            backend._ssh = SshCli(backend._t, runner=runner)
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )
            await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 1
            assert rm_calls  # the remote dir was removed
            remaining = await db.get_open_execution_handles()
            assert all(h["execution_id"] != "e1" for h in remaining)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_terminal_handles_docker_unaffected_by_ssh_branch(
        self, tmp_path
    ) -> None:
        """CRITICAL — zero regression: a docker `terminal` row is still
        swept exactly as before (unaffected by the new ssh branch)."""
        orch, db = await _orch_with_db(tmp_path)
        docker = MagicMock()
        docker.ps_ids_by_label = AsyncMock(return_value=[])
        orch._docker = docker
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="docker",
                transport_ref="docker:maestro-e1",
                attempt=1,
            )
            await db.mark_execution_state("e1", "terminal", allowed_from=["prepared"])

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 1
            remaining = await db.get_open_execution_handles()
            assert all(h["execution_id"] != "e1" for h in remaining)
        finally:
            await db.close()
