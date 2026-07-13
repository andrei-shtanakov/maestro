"""Tests for the Workspace Manager module."""

import os
import subprocess as sp
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from maestro.git import GitManager, WorktreeError
from maestro.workspace import (
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceManager,
    WorkspaceNotFoundError,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def workspace_mgr(
    git_repo: Path,
    temp_dir: Path,
) -> WorkspaceManager:
    """Create a WorkspaceManager backed by a real git repo."""
    git_mgr = GitManager(git_repo)
    workspace_base = temp_dir / "workspaces"
    return WorkspaceManager(git_manager=git_mgr, workspace_base=workspace_base)


# =============================================================================
# Unit Tests: Create Workspace
# =============================================================================


class TestCreateWorkspace:
    """Tests for create_workspace functionality."""

    @pytest.mark.integration
    def test_create_workspace_success(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that create_workspace creates a worktree directory."""
        result = workspace_mgr.create_workspace(
            "workstream-001",
            "agent/workstream-001",
        )

        assert result.exists()
        assert result.is_dir()
        assert result.name == "workstream-001"
        # The worktree should contain the repo files
        assert (result / "README.md").exists()

    @pytest.mark.integration
    def test_create_workspace_returns_correct_path(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that create_workspace returns path under workspace_base."""
        result = workspace_mgr.create_workspace(
            "workstream-002",
            "agent/workstream-002",
        )

        expected = workspace_mgr.workspace_base / "workstream-002"
        assert result == expected

    @pytest.mark.integration
    def test_create_workspace_already_exists_error(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that creating a workspace in an existing dir raises error."""
        # Create the directory manually so it already exists
        workspace_path = workspace_mgr.workspace_base / "workstream-dup"
        workspace_path.mkdir(parents=True)

        with pytest.raises(
            WorkspaceExistsError,
            match="already exists",
        ):
            workspace_mgr.create_workspace(
                "workstream-dup",
                "agent/workstream-dup",
            )

    @pytest.mark.integration
    def test_create_workspace_worktree_failure(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that WorktreeError from git is wrapped in WorkspaceError."""
        with (
            patch.object(
                workspace_mgr._git,
                "create_worktree",
                side_effect=WorktreeError("git worktree add failed"),
            ),
            pytest.raises(
                WorkspaceError,
                match="Failed to create workspace",
            ),
        ):
            workspace_mgr.create_workspace(
                "workstream-fail",
                "agent/workstream-fail",
            )

    @pytest.mark.integration
    def test_create_workspace_creates_base_dir(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that create_workspace creates workspace_base if missing."""
        assert not workspace_mgr.workspace_base.exists()

        workspace_mgr.create_workspace(
            "workstream-first",
            "agent/workstream-first",
        )

        assert workspace_mgr.workspace_base.exists()


# =============================================================================
# Unit Tests: Setup Spec Runner
# =============================================================================


class TestSetupSpecRunner:
    """Tests for setup_spec_runner functionality."""

    @pytest.mark.integration
    def test_setup_spec_runner_writes_config_file(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that setup_spec_runner writes executor.config.yaml."""
        workspace_path = workspace_mgr.create_workspace(
            "workstream-spec",
            "agent/workstream-spec",
        )
        config = {"mode": "test", "timeout": 60}

        workspace_mgr.setup_spec_runner(workspace_path, config)

        config_file = workspace_path / "spec-runner.config.yaml"
        assert config_file.exists()

        with config_file.open() as f:
            loaded = yaml.safe_load(f)
        assert loaded == {"mode": "test", "timeout": 60}

    @pytest.mark.integration
    def test_setup_spec_runner_creates_spec_dir(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that setup_spec_runner creates spec/ directory."""
        workspace_path = workspace_mgr.create_workspace(
            "workstream-specdir",
            "agent/workstream-specdir",
        )

        workspace_mgr.setup_spec_runner(workspace_path, {"key": "value"})

        spec_dir = workspace_path / "spec"
        assert spec_dir.exists()
        assert spec_dir.is_dir()

    def test_setup_spec_runner_workspace_not_found(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that setup_spec_runner raises if workspace missing."""
        nonexistent = workspace_mgr.workspace_base / "does-not-exist"

        with pytest.raises(
            WorkspaceNotFoundError,
            match="Workspace not found",
        ):
            workspace_mgr.setup_spec_runner(nonexistent, {"key": "val"})

    @pytest.mark.integration
    def test_setup_spec_runner_idempotent_spec_dir(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that calling setup_spec_runner twice doesn't fail."""
        workspace_path = workspace_mgr.create_workspace(
            "workstream-idem",
            "agent/workstream-idem",
        )
        config = {"run": True}

        workspace_mgr.setup_spec_runner(workspace_path, config)
        # Second call should not raise
        workspace_mgr.setup_spec_runner(workspace_path, config)

        assert (workspace_path / "spec").is_dir()
        assert (workspace_path / "spec-runner.config.yaml").exists()

    @pytest.mark.integration
    def test_setup_spec_runner_cleans_stale_dot_prefixed_state(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """H-8: a stale dot-before-prefix lock/history inherited from a prior
        run must be cleaned — a leftover spec/.maestro-spec.lock would block
        the next spec-runner."""
        workspace_path = workspace_mgr.create_workspace(
            "workstream-stale",
            "agent/workstream-stale",
        )
        spec = workspace_path / "spec"
        spec.mkdir(exist_ok=True)
        (spec / ".maestro-spec.lock").write_text("PID: 999")
        (spec / ".maestro-task-history.log").write_text("stale")

        workspace_mgr.setup_spec_runner(workspace_path, {"run": True})

        assert not (spec / ".maestro-spec.lock").exists()
        assert not (spec / ".maestro-task-history.log").exists()


# =============================================================================
# Unit Tests: Cleanup Workspace
# =============================================================================


class TestCleanupWorkspace:
    """Tests for cleanup_workspace functionality."""

    @pytest.mark.integration
    def test_cleanup_workspace_success(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup_workspace removes the worktree directory."""
        workspace_mgr.create_workspace(
            "workstream-clean",
            "agent/workstream-clean",
        )
        assert workspace_mgr.workspace_exists("workstream-clean")

        workspace_mgr.cleanup_workspace("workstream-clean")

        assert not workspace_mgr.workspace_exists("workstream-clean")

    def test_cleanup_workspace_already_cleaned_noop(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleaning a non-existent workspace is a no-op."""
        # Should not raise
        workspace_mgr.cleanup_workspace("never-existed")

    @pytest.mark.integration
    def test_cleanup_workspace_fallback_to_shutil(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup falls back to shutil when git remove fails."""
        workspace_path = workspace_mgr.create_workspace(
            "workstream-fallback",
            "agent/workstream-fallback",
        )
        assert workspace_path.exists()

        with (
            patch.object(
                workspace_mgr._git,
                "remove_worktree",
                side_effect=WorktreeError("remove failed"),
            ),
            patch.object(
                workspace_mgr._git,
                "prune_worktrees",
            ) as mock_prune,
        ):
            workspace_mgr.cleanup_workspace("workstream-fallback")

        # shutil.rmtree should have removed it
        assert not workspace_path.exists()
        mock_prune.assert_called_once()


# =============================================================================
# Unit Tests: Get Workspace Path
# =============================================================================


class TestGetWorkspacePath:
    """Tests for get_workspace_path functionality."""

    @pytest.mark.integration
    def test_get_workspace_path_success(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test get_workspace_path returns correct path for existing workspace."""
        created = workspace_mgr.create_workspace(
            "workstream-get",
            "agent/workstream-get",
        )

        result = workspace_mgr.get_workspace_path("workstream-get")

        assert result == created
        assert result.exists()

    def test_get_workspace_path_not_found(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test get_workspace_path raises when workspace does not exist."""
        with pytest.raises(
            WorkspaceNotFoundError,
            match="Workspace not found",
        ):
            workspace_mgr.get_workspace_path("nonexistent")


# =============================================================================
# Unit Tests: Workspace Exists
# =============================================================================


class TestWorkspaceExists:
    """Tests for workspace_exists functionality."""

    @pytest.mark.integration
    def test_workspace_exists_true(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test workspace_exists returns True for existing workspace."""
        workspace_mgr.create_workspace(
            "workstream-exists",
            "agent/workstream-exists",
        )

        assert workspace_mgr.workspace_exists("workstream-exists") is True

    def test_workspace_exists_false(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test workspace_exists returns False for absent workspace."""
        assert workspace_mgr.workspace_exists("no-such-workstream") is False


# =============================================================================
# Unit Tests: List Workspaces
# =============================================================================


class TestListWorkspaces:
    """Tests for list_workspaces functionality."""

    def test_list_workspaces_empty(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test list_workspaces returns empty list when base doesn't exist."""
        assert workspace_mgr.list_workspaces() == []

    @pytest.mark.integration
    def test_list_workspaces_with_workspaces(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test list_workspaces returns existing workspace directories."""
        workspace_mgr.create_workspace("workstream-a", "agent/workstream-a")
        workspace_mgr.create_workspace("workstream-b", "agent/workstream-b")

        workspaces = workspace_mgr.list_workspaces()

        names = [w.name for w in workspaces]
        assert "workstream-a" in names
        assert "workstream-b" in names
        assert len(workspaces) == 2

    def test_list_workspaces_base_not_exists(
        self,
        temp_dir: Path,
    ) -> None:
        """Test list_workspaces returns empty when base dir missing."""
        git_mgr = MagicMock(spec=GitManager)
        nonexistent_base = temp_dir / "missing_base"
        mgr = WorkspaceManager(
            git_manager=git_mgr,
            workspace_base=nonexistent_base,
        )

        result = mgr.list_workspaces()

        assert result == []

    @pytest.mark.integration
    def test_list_workspaces_ignores_hidden_dirs(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that list_workspaces skips directories starting with dot."""
        workspace_mgr.create_workspace("workstream-vis", "agent/workstream-vis")

        # Create a hidden directory manually
        hidden = workspace_mgr.workspace_base / ".hidden"
        hidden.mkdir()

        workspaces = workspace_mgr.list_workspaces()

        names = [w.name for w in workspaces]
        assert "workstream-vis" in names
        assert ".hidden" not in names

    @pytest.mark.integration
    def test_list_workspaces_sorted(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that list_workspaces returns directories in sorted order."""
        workspace_mgr.create_workspace("workstream-c", "agent/workstream-c")
        workspace_mgr.create_workspace("workstream-a", "agent/workstream-a")
        workspace_mgr.create_workspace("workstream-b", "agent/workstream-b")

        workspaces = workspace_mgr.list_workspaces()
        names = [w.name for w in workspaces]

        assert names == sorted(names)


# =============================================================================
# Integration Tests: Cleanup All
# =============================================================================


class TestCleanupAll:
    """Tests for cleanup_all functionality."""

    @pytest.mark.integration
    def test_cleanup_all_removes_all_workspaces(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup_all removes every workspace."""
        workspace_mgr.create_workspace("workstream-x", "agent/workstream-x")
        workspace_mgr.create_workspace("workstream-y", "agent/workstream-y")

        assert len(workspace_mgr.list_workspaces()) == 2

        workspace_mgr.cleanup_all()

        assert len(workspace_mgr.list_workspaces()) == 0
        assert not workspace_mgr.workspace_exists("workstream-x")
        assert not workspace_mgr.workspace_exists("workstream-y")

    def test_cleanup_all_empty_is_noop(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup_all with no workspaces does nothing."""
        # Should not raise
        workspace_mgr.cleanup_all()

        assert workspace_mgr.list_workspaces() == []


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    sp.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    return repo


class TestEnsureHarnessExcludes:
    """H-7: harness artifacts are kept untracked via the repo-local
    $GIT_COMMON_DIR/info/exclude (shared by all linked worktrees)."""

    def test_appends_block_with_narrow_patterns(self, tmp_path: Path) -> None:
        from maestro.workspace import ensure_harness_excludes

        repo = _init_repo(tmp_path)
        ensure_harness_excludes(repo)
        content = (repo / ".git" / "info" / "exclude").read_text()
        assert "spec/maestro-*" in content
        assert "spec/.executor-*" in content
        assert "/spec-runner.config.yaml" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        from maestro.workspace import ensure_harness_excludes

        repo = _init_repo(tmp_path)
        ensure_harness_excludes(repo)
        once = (repo / ".git" / "info" / "exclude").read_text()
        ensure_harness_excludes(repo)
        assert (repo / ".git" / "info" / "exclude").read_text() == once

    def test_worktree_path_resolves_to_common_dir(self, tmp_path: Path) -> None:
        from maestro.workspace import ensure_harness_excludes

        repo = _init_repo(tmp_path)
        wt = tmp_path / "wt"
        sp.run(
            ["git", "-C", str(repo), "worktree", "add", str(wt), "-b", "f/x"],
            check=True,
            capture_output=True,
        )
        ensure_harness_excludes(wt)  # called with the WORKTREE path
        content = (repo / ".git" / "info" / "exclude").read_text()
        assert "spec/maestro-*" in content

    def test_untracked_harness_file_is_ignored(self, tmp_path: Path) -> None:
        from maestro.workspace import ensure_harness_excludes

        repo = _init_repo(tmp_path)
        ensure_harness_excludes(repo)
        spec = repo / "spec"
        spec.mkdir()
        (spec / "maestro-tasks.md").write_text("x")
        (repo / "spec-runner.config.yaml").write_text("x")
        status = sp.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "maestro-tasks.md" not in status.stdout
        assert "spec-runner.config.yaml" not in status.stdout

    def test_non_repo_raises(self, tmp_path: Path) -> None:
        from maestro.workspace import WorkspaceError, ensure_harness_excludes

        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorkspaceError):
            ensure_harness_excludes(plain)

    def test_dot_prefixed_harness_files_ignored(self, tmp_path: Path) -> None:
        """H-8: spec-runner names task-history/spec.lock with the dot BEFORE
        the prefix (spec/.maestro-task-history.log, spec/.maestro-spec.lock);
        those must be ignored too, not just spec/maestro-* and .executor-*."""
        from maestro.workspace import ensure_harness_excludes

        repo = _init_repo(tmp_path)
        ensure_harness_excludes(repo)
        content = (repo / ".git" / "info" / "exclude").read_text()
        assert "spec/.maestro-*" in content

        spec = repo / "spec"
        spec.mkdir()
        (spec / ".maestro-task-history.log").write_text("x")
        (spec / ".maestro-spec.lock").write_text("x")
        status = sp.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert ".maestro-task-history.log" not in status.stdout
        assert ".maestro-spec.lock" not in status.stdout

    def test_stale_block_refreshed_in_place(self, tmp_path: Path) -> None:
        """H-8 migration: a repo touched by an older gates version carries an
        outdated block; re-running must replace it in place (not no-op), so
        the new patterns reach repos already seen — without duplicating."""
        from maestro.workspace import (
            _EXCLUDE_BEGIN,
            _EXCLUDE_END,
            ensure_harness_excludes,
        )

        repo = _init_repo(tmp_path)
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        # Simulate the old v1.2 block (no spec/.maestro-* pattern).
        old_block = "\n".join(
            [_EXCLUDE_BEGIN, "spec/maestro-*", "spec/.executor-*", _EXCLUDE_END]
        )
        exclude.write_text(f"# user rule\n*.log\n{old_block}\n", encoding="utf-8")

        ensure_harness_excludes(repo)
        content = exclude.read_text()
        assert "spec/.maestro-*" in content  # new pattern picked up
        assert content.count(_EXCLUDE_BEGIN) == 1  # replaced, not duplicated
        assert "# user rule" in content and "*.log" in content  # user lines kept
