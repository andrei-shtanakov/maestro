"""Tests for the Orchestrator class.

This module contains unit tests for the multi-process orchestrator,
covering initialization, workstream resolution, failure handling,
PR body formatting, and shutdown behavior.
"""

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from maestro.models import (
    OrchestratorConfig,
    Workstream,
    WorkstreamConfig,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator, OrchestratorError


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def orch_config() -> OrchestratorConfig:
    """Provide an OrchestratorConfig for testing."""
    return OrchestratorConfig(
        project="test-project",
        repo_url="https://github.com/test/repo",
        repo_path="/tmp/test-repo",
        workspace_base="/tmp/test-ws",
        max_concurrent=2,
    )


@pytest.fixture
def mock_db() -> MagicMock:
    """Provide a mock Database with async methods."""
    db = MagicMock()
    type(db).is_connected = PropertyMock(return_value=True)
    db.get_all_workstreams = AsyncMock(return_value=[])
    db.get_workstreams_by_status = AsyncMock(return_value=[])
    db.create_workstream = AsyncMock()
    db.get_workstream = AsyncMock()
    db.update_workstream_status = AsyncMock()
    return db


@pytest.fixture
def mock_workspace_mgr() -> MagicMock:
    """Provide a mock WorkspaceManager."""
    mgr = MagicMock()
    mgr.create_workspace = MagicMock(return_value=Path("/tmp/test-ws/z1"))
    mgr.get_workspace_path = MagicMock(return_value=Path("/tmp/test-ws/z1"))
    mgr.workspace_exists = MagicMock(return_value=False)
    mgr.cleanup_workspace = MagicMock()
    return mgr


@pytest.fixture
def mock_decomposer() -> MagicMock:
    """Provide a mock ProjectDecomposer."""
    decomposer = MagicMock()
    decomposer.decompose = MagicMock(return_value=[])
    decomposer.generate_spec = AsyncMock()
    return decomposer


@pytest.fixture
def mock_pr_manager() -> MagicMock:
    """Provide a mock PRManager."""
    mgr = MagicMock()
    mgr.push_and_create_pr = MagicMock(
        return_value="https://github.com/test/repo/pull/1"
    )
    return mgr


@pytest.fixture
def orchestrator(
    mock_db: MagicMock,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
    orch_config: OrchestratorConfig,
) -> Orchestrator:
    """Provide an Orchestrator instance with all mocked dependencies."""
    return Orchestrator(
        db=mock_db,
        workspace_mgr=mock_workspace_mgr,
        decomposer=mock_decomposer,
        pr_manager=mock_pr_manager,
        config=orch_config,
        log_dir=Path("/tmp/test-logs"),
    )


def _make_workstream(
    workstream_id: str = "z1",
    title: str = "Test Workstream",
    description: str = "A test workstream",
    branch: str = "feature/z1",
    status: WorkstreamStatus = WorkstreamStatus.PENDING,
    depends_on: list[str] | None = None,
    priority: int = 0,
    retry_count: int = 0,
    max_retries: int = 2,
    scope: list[str] | None = None,
    subtask_progress: str | None = None,
) -> Workstream:
    """Create a Workstream instance for testing."""
    return Workstream(
        id=workstream_id,
        title=title,
        description=description,
        branch=branch,
        status=status,
        depends_on=depends_on or [],
        priority=priority,
        retry_count=retry_count,
        max_retries=max_retries,
        scope=scope if scope is not None else ["src/**/*.py"],
        subtask_progress=subtask_progress,
    )


# =============================================================================
# Initialization Tests
# =============================================================================


class TestOrchestratorInit:
    """Tests for Orchestrator.__init__."""

    def test_init_stores_dependencies(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        orch_config: OrchestratorConfig,
    ) -> None:
        """Test that __init__ stores all injected dependencies."""
        assert orchestrator._db is mock_db
        assert orchestrator._workspace_mgr is mock_workspace_mgr
        assert orchestrator._decomposer is mock_decomposer
        assert orchestrator._pr_manager is mock_pr_manager
        assert orchestrator._config is orch_config

    def test_init_default_log_dir(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        orch_config: OrchestratorConfig,
    ) -> None:
        """Test that log_dir defaults to repo_path/logs when not specified."""
        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=orch_config,
        )
        expected = Path(orch_config.repo_path).expanduser() / "logs"
        assert orch._log_dir == expected

    def test_init_custom_log_dir(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that a custom log_dir is stored correctly."""
        assert orchestrator._log_dir == Path("/tmp/test-logs")

    def test_init_empty_running_dict(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that the running dict starts empty."""
        assert orchestrator._running == {}

    def test_init_shutdown_not_requested(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that shutdown is not requested at init."""
        assert orchestrator._shutdown_requested is False

    def test_init_loop_is_none(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that the event loop is None at init."""
        assert orchestrator._loop is None

    def test_is_running_false_at_init(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that is_running is False before run() is called."""
        assert orchestrator.is_running is False


def _ws(
    zid: str,
    status: WorkstreamStatus,
    *,
    retry_count: int = 0,
    max_retries: int = 3,
) -> Workstream:
    """Create a minimal valid Workstream for background-generation tests."""
    return Workstream(
        id=zid,
        title=zid,
        description="d",
        scope=["s"],
        branch=f"feature/{zid}",
        status=status,
        retry_count=retry_count,
        max_retries=max_retries,
    )


# =============================================================================
# _ensure_workstreams Tests
# =============================================================================


class TestEnsureWorkstreams:
    """Tests for Orchestrator._ensure_workstreams."""

    @pytest.mark.anyio
    async def test_existing_workstreams_noop(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that existing workstreams in DB results in no-op."""
        existing = [_make_workstream("z1"), _make_workstream("z2")]
        mock_db.get_all_workstreams = AsyncMock(return_value=existing)

        await orchestrator._ensure_workstreams()

        mock_db.create_workstream.assert_not_called()
        assert orchestrator._stats.total_workstreams == 2

    @pytest.mark.anyio
    async def test_workstreams_from_config_creates_in_db(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        """Test that workstreams from config are created in the database."""
        workstreams_configs = [
            WorkstreamConfig(
                id="z1",
                title="First",
                description="First workstream",
                scope=["src/**"],
            ),
            WorkstreamConfig(
                id="z2",
                title="Second",
                description="Second workstream",
                scope=["tests/**"],
            ),
        ]
        config = OrchestratorConfig(
            project="test-project",
            repo_url="https://github.com/test/repo",
            repo_path="/tmp/test-repo",
            workspace_base="/tmp/test-ws",
            max_concurrent=2,
            workstreams=workstreams_configs,
        )

        mock_db.get_all_workstreams = AsyncMock(return_value=[])

        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=config,
            log_dir=Path("/tmp/test-logs"),
        )

        await orch._ensure_workstreams()

        assert mock_db.create_workstream.call_count == 2
        assert orch._stats.total_workstreams == 2
        mock_decomposer.decompose.assert_not_called()

    @pytest.mark.anyio
    async def test_auto_decompose_from_description(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        """Test auto-decomposition when description is provided and no workstreams."""
        config = OrchestratorConfig(
            project="test-project",
            description="Build a REST API with auth and CRUD",
            repo_url="https://github.com/test/repo",
            repo_path="/tmp/test-repo",
            workspace_base="/tmp/test-ws",
            max_concurrent=2,
        )

        decomposed = [
            WorkstreamConfig(
                id="auth",
                title="Auth module",
                description="Add authentication",
                scope=["auth/**"],
            ),
            WorkstreamConfig(
                id="crud",
                title="CRUD module",
                description="Add CRUD endpoints",
                scope=["api/**"],
            ),
        ]
        mock_decomposer.decompose = MagicMock(return_value=decomposed)
        mock_db.get_all_workstreams = AsyncMock(return_value=[])

        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=config,
            log_dir=Path("/tmp/test-logs"),
        )

        await orch._ensure_workstreams()

        mock_decomposer.decompose.assert_called_once_with(
            "Build a REST API with auth and CRUD"
        )
        assert mock_db.create_workstream.call_count == 2
        assert orch._stats.total_workstreams == 2

    @pytest.mark.anyio
    async def test_no_workstreams_no_description_raises(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that no workstreams and no description raises OrchestratorError."""
        mock_db.get_all_workstreams = AsyncMock(return_value=[])
        # orch_config has no workstreams and description defaults to ""

        with pytest.raises(OrchestratorError, match="No workstreams in config"):
            await orchestrator._ensure_workstreams()


# =============================================================================
# _resolve_ready Tests
# =============================================================================


class TestResolveReady:
    """Tests for Orchestrator._resolve_ready."""

    @pytest.mark.anyio
    async def test_pending_no_deps_becomes_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a pending workstream with no deps is resolved as ready."""
        workstream = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=[],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[workstream])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_workstream_with_unmet_deps_not_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with unmet dependencies is not ready."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=["z0"],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_workstream_with_met_deps_becomes_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with all deps completed is ready."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=["z0"],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids={"z0"})

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_already_running_not_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream currently running is not resolved as ready."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=[],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        # Simulate z1 being in _running
        orchestrator._running["z1"] = MagicMock()

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_non_pending_non_ready_status_excluded(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that workstreams in non-pending/ready states are excluded."""
        z_done = _make_workstream("z1", status=WorkstreamStatus.DONE)
        z_failed = _make_workstream("z2", status=WorkstreamStatus.FAILED)
        z_running = _make_workstream("z3", status=WorkstreamStatus.RUNNING)
        mock_db.get_all_workstreams = AsyncMock(
            return_value=[z_done, z_failed, z_running]
        )

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_ready_status_workstream_resolved(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a READY-status workstream (not just PENDING) is resolved."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.READY,
            depends_on=[],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_priority_sorting_descending(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that ready workstreams are sorted by priority descending."""
        z_low = _make_workstream(
            "z-low",
            status=WorkstreamStatus.PENDING,
            priority=1,
        )
        z_high = _make_workstream(
            "z-high",
            status=WorkstreamStatus.PENDING,
            priority=10,
        )
        z_mid = _make_workstream(
            "z-mid",
            status=WorkstreamStatus.PENDING,
            priority=5,
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z_low, z_high, z_mid])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z-high", "z-mid", "z-low"]

    @pytest.mark.anyio
    async def test_multiple_deps_all_must_be_met(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that all deps must be completed, not just some."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=["dep-a", "dep-b"],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        # Only one dep met
        ready = await orchestrator._resolve_ready(completed_ids={"dep-a"})
        assert ready == []

        # Both deps met
        ready = await orchestrator._resolve_ready(completed_ids={"dep-a", "dep-b"})
        assert ready == ["z1"]


# =============================================================================
# _handle_failure Tests
# =============================================================================


class TestHandleFailure:
    """Tests for Orchestrator._handle_failure."""

    @pytest.mark.anyio
    async def test_retry_when_retries_left(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with retries left is set back to READY."""
        workstream = _make_workstream(
            "z1",
            retry_count=0,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "spec-runner exited with code 1")

        # Should transition to FAILED first, then READY
        calls = mock_db.update_workstream_status.call_args_list
        assert len(calls) == 2

        # First call: mark as FAILED with error + incremented retry count
        first_call_args = calls[0]
        assert first_call_args[0] == ("z1", WorkstreamStatus.FAILED)
        assert first_call_args[1]["error_message"] == ("spec-runner exited with code 1")
        assert first_call_args[1]["retry_count"] == 1

        # Second call: mark as READY
        second_call_args = calls[1]
        assert second_call_args[0] == ("z1", WorkstreamStatus.READY)
        assert second_call_args[1]["expected_status"] == WorkstreamStatus.FAILED

    @pytest.mark.anyio
    async def test_needs_review_when_no_retries(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with no retries left goes to NEEDS_REVIEW."""
        workstream = _make_workstream(
            "z1",
            retry_count=2,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "spec-runner exited with code 1")

        calls = mock_db.update_workstream_status.call_args_list
        assert len(calls) == 2

        # First call: mark as FAILED
        first_call_args = calls[0]
        assert first_call_args[0] == ("z1", WorkstreamStatus.FAILED)
        assert first_call_args[1]["error_message"] == ("spec-runner exited with code 1")

        # Second call: mark as NEEDS_REVIEW
        second_call_args = calls[1]
        assert second_call_args[0] == ("z1", WorkstreamStatus.NEEDS_REVIEW)
        assert second_call_args[1]["expected_status"] == WorkstreamStatus.FAILED

    @pytest.mark.anyio
    async def test_failure_increments_stats(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that failure with no retries increments the failed stat."""
        workstream = _make_workstream(
            "z1",
            retry_count=2,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "error")

        assert orchestrator._stats.failed == 1

    @pytest.mark.anyio
    async def test_retry_does_not_increment_failed_stats(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that retry does not increment the failed stat."""
        workstream = _make_workstream(
            "z1",
            retry_count=0,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "error")

        assert orchestrator._stats.failed == 0


# =============================================================================
# _build_pr_body Tests
# =============================================================================


class TestBuildPrBody:
    """Tests for Orchestrator._build_pr_body."""

    def test_formats_correctly(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that PR body is formatted with summary, scope, and progress."""
        workstream = _make_workstream(
            "z1",
            description="Implement user auth",
            scope=["src/auth/**", "tests/auth/**"],
            subtask_progress="3/5 done",
        )

        body = orchestrator._build_pr_body(workstream)

        assert "## Summary" in body
        assert "Implement user auth" in body
        assert "## Scope" in body
        assert "- `src/auth/**`" in body
        assert "- `tests/auth/**`" in body
        assert "## Progress" in body
        assert "3/5 done" in body
        assert "Generated by Maestro Orchestrator" in body

    def test_no_progress_shows_na(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that missing progress shows N/A."""
        workstream = _make_workstream(
            "z1",
            description="Some work",
            scope=["src/**"],
            subtask_progress=None,
        )

        body = orchestrator._build_pr_body(workstream)

        assert "N/A" in body

    def test_empty_scope(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test PR body with empty scope list."""
        workstream = _make_workstream(
            "z1",
            description="Global changes",
            scope=[],
        )

        body = orchestrator._build_pr_body(workstream)

        assert "## Scope" in body
        # Scope section should be empty (no bullet items)
        assert "- `" not in body


# =============================================================================
# shutdown Tests
# =============================================================================


class TestShutdown:
    """Tests for Orchestrator.shutdown."""

    @pytest.mark.anyio
    async def test_shutdown_sets_event(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that shutdown sets the shutdown event and flag."""
        assert not orchestrator._shutdown_event.is_set()
        assert orchestrator._shutdown_requested is False

        await orchestrator.shutdown()

        assert orchestrator._shutdown_event.is_set()
        assert orchestrator._shutdown_requested is True

    @pytest.mark.anyio
    async def test_shutdown_idempotent(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that calling shutdown multiple times is safe."""
        await orchestrator.shutdown()
        await orchestrator.shutdown()

        assert orchestrator._shutdown_event.is_set()
        assert orchestrator._shutdown_requested is True


# =============================================================================
# Background generation Tests
# =============================================================================


class TestBackgroundGeneration:
    """Tests for async background-task spec generation (_spawn_ready,
    _generate_and_launch, _cleanup)."""

    @pytest.mark.anyio
    async def test_spawn_ready_does_not_block_on_generation(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """generate_spec is launched as a background task; _spawn_ready
        returns before it completes."""
        import asyncio

        gate = asyncio.Event()

        async def slow_generate(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=slow_generate)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        # generation still in flight, but _spawn_ready already returned:
        assert "z1" in orchestrator._generating
        assert not orchestrator._generating["z1"].done()
        gate.set()
        await orchestrator._generating["z1"]  # let it finish/cleanup

    @pytest.mark.anyio
    async def test_slot_accounting_counts_generating(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """max_concurrent bounds generating + running (no overspawn)."""
        import asyncio

        orchestrator._config.max_concurrent = 2
        gate = asyncio.Event()

        async def block(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=block)
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1", "z2", "z3"])
        assert len(orchestrator._generating) == 2  # z3 held back
        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(Exception):
                await t

    @pytest.mark.anyio
    async def test_existing_generating_reduces_available_slots(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        orchestrator._config.max_concurrent = 2
        # one slot already taken by an in-flight generation
        orchestrator._generating["busy"] = asyncio.create_task(asyncio.sleep(3600))
        gate = asyncio.Event()

        async def block(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=block)
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )
        await orchestrator._spawn_ready(["z1", "z2"])
        # only 1 free slot (2 - 0 running - 1 generating) → exactly one launched
        assert len([k for k in orchestrator._generating if k != "busy"]) == 1
        orchestrator._generating["busy"].cancel()
        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    @pytest.mark.anyio
    async def test_generation_failure_routes_through_handle_failure(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        from maestro.decomposer import DecomposerError

        mock_decomposer.generate_spec = AsyncMock(side_effect=DecomposerError("nope"))
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY, retry_count=0, max_retries=2)
        )
        orchestrator._handle_failure = AsyncMock()

        # pre-seed as _spawn_ready would, so the finally-pop is actually tested
        orchestrator._generating["z1"] = asyncio.create_task(asyncio.sleep(0))
        await orchestrator._generate_and_launch("z1")
        orchestrator._handle_failure.assert_awaited_once()
        assert "z1" not in orchestrator._generating  # genuinely freed in finally

    @pytest.mark.anyio
    async def test_shutdown_cancels_generation_back_to_ready(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        started = asyncio.Event()

        async def hang(*a, **k):
            started.set()
            await asyncio.sleep(3600)

        mock_decomposer.generate_spec = AsyncMock(side_effect=hang)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        await started.wait()
        await orchestrator._cleanup()
        # generation task cancelled, workstream returned to READY (no retry used)
        calls = [c.args for c in mock_db.update_workstream_status.await_args_list]
        assert any(WorkstreamStatus.READY in c for c in calls)

    @pytest.mark.anyio
    async def test_happy_path_registers_in_running_before_pid_update(
        self, orchestrator, mock_db, mock_decomposer, tmp_path
    ) -> None:
        """Full success path: generate_spec succeeds, the process is
        spawned, and _spawn_workstream must land the workstream in
        `_running` (with `_generating` emptied).

        Regression test for the shutdown-orphan bug: the pid DB update
        used to happen *before* the `_running` registration, so a
        cancellation landing between those two awaits left the spawned
        `run --all` process untracked by `_cleanup`. Registration now
        happens first, so this success-path assertion also pins down
        that ordering (see `test_shutdown_cancels_generation_back_to_ready`
        for the cancellation side of the invariant).
        """
        from unittest.mock import patch

        # Route log file + workspace through a real tmp dir so os.open()
        # and the (mocked-out) commit step don't touch the repo.
        orchestrator._log_dir = tmp_path
        workspace = tmp_path / "ws-z1"
        workspace.mkdir()
        orchestrator._workspace_mgr.workspace_exists = MagicMock(return_value=True)
        orchestrator._workspace_mgr.get_workspace_path = MagicMock(
            return_value=workspace
        )
        orchestrator._commit_spec_in_workspace = MagicMock()

        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        fake_process = MagicMock()
        fake_process.pid = 4242

        with patch(
            "maestro.orchestrator.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake_process),
        ):
            await orchestrator._spawn_ready(["z1"])
            await orchestrator._generating["z1"]

        assert "z1" not in orchestrator._generating
        assert "z1" in orchestrator._running
        assert orchestrator._running["z1"].process is fake_process

    @pytest.mark.anyio
    async def test_cleanup_terminates_process_when_cancel_hits_pid_update(
        self, orchestrator, mock_db, mock_decomposer, tmp_path
    ) -> None:
        """Discriminating regression guard for the shutdown-orphan window.

        Suspend `_spawn_workstream` exactly on the `process_pid` DB
        update — AFTER the process is spawned and registered in
        `_running`. Then shut down. `_cleanup` must find the process in
        `_running` and terminate it. This FAILS if registration is moved
        back to AFTER the pid update: on cancel the process would not yet
        be in `_running`, `_cleanup` would never touch it, and it would
        survive as an orphan.
        """
        import asyncio
        from unittest.mock import patch

        orchestrator._log_dir = tmp_path
        orchestrator._shutdown_grace_seconds = 0  # no 5s sleep in _cleanup
        workspace = tmp_path / "ws-z1"
        workspace.mkdir()
        orchestrator._workspace_mgr.workspace_exists = MagicMock(return_value=True)
        orchestrator._workspace_mgr.get_workspace_path = MagicMock(
            return_value=workspace
        )
        orchestrator._commit_spec_in_workspace = MagicMock()
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        # Fake process mirroring how _cleanup drives it:
        # terminate() -> sleep(grace) -> returncode check -> wait().
        fake_process = MagicMock()
        fake_process.pid = 4242
        fake_process.returncode = 0  # already exited: skip kill()
        fake_process.terminate = MagicMock()
        fake_process.kill = MagicMock()
        fake_process.wait = AsyncMock(return_value=0)

        # Suspend on the RUNNING+pid update (fired right after registration),
        # signalling `reached` so the test can proceed to shutdown.
        reached = asyncio.Event()

        async def hang_on_pid(*args, **kwargs):
            if kwargs.get("process_pid") is not None:
                reached.set()
                await asyncio.sleep(3600)  # hang here until cancelled
            return None

        mock_db.update_workstream_status = AsyncMock(side_effect=hang_on_pid)

        with patch(
            "maestro.orchestrator.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake_process),
        ):
            await orchestrator._spawn_ready(["z1"])
            await reached.wait()  # generation is now parked on the pid update
            # Process is spawned + registered but the pid-update await is
            # still pending — the exact orphan window.
            assert "z1" in orchestrator._running
            await orchestrator._cleanup()

        # _cleanup found the process in _running and terminated it.
        fake_process.terminate.assert_called_once()
        assert "z1" not in orchestrator._running

    @pytest.mark.anyio
    async def test_spawn_ready_does_not_duplicate_in_flight_generation(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """A workstream already in `_generating` must not be re-spawned.

        Regression guard: `_resolve_ready` used to exclude only
        `_running`, not `_generating`, so a workstream whose generation
        task had been created but whose DB status hadn't yet flipped to
        DECOMPOSING would be returned as ready again on the next loop
        tick. `_spawn_ready` then overwrote the `_generating[zid]` entry
        with a second task, leaking the first task reference and running
        spec generation twice for the same workstream.
        """
        import asyncio

        gate = asyncio.Event()

        async def block(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=block)
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        first = orchestrator._generating["z1"]
        await asyncio.sleep(0)  # let the task run up to the blocked call

        # Second tick before z1 flips to DECOMPOSING: must not overwrite.
        await orchestrator._spawn_ready(["z1"])
        assert orchestrator._generating["z1"] is first
        assert len(orchestrator._generating) == 1
        assert mock_decomposer.generate_spec.call_count == 1

        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
