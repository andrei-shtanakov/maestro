"""Workspace management for multi-process orchestration.

This module provides WorkspaceManager for creating and managing
isolated git worktrees for each workstream (independent work unit).
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from maestro.git import GitManager, WorktreeError
from maestro.models import SPEC_PREFIX


class WorkspaceError(Exception):
    """Base exception for workspace operations."""


class WorkspaceExistsError(WorkspaceError):
    """Raised when workspace directory already exists."""


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when workspace directory not found."""


# H-7: every file Maestro generates inside a target repo's worktree stays
# untracked. Ignored files survive spec-runner's subtask branching/merges
# and are invisible to the agent's `git add -A` auto-commit, so no harness
# artifact ever reaches the feature branch or the PR.
_EXCLUDE_BEGIN = "# >>> maestro orchestrator artifacts (auto-managed) >>>"
_EXCLUDE_END = "# <<< maestro orchestrator artifacts <<<"


def _exclude_patterns() -> tuple[str, ...]:
    return (
        f"spec/{SPEC_PREFIX}*",
        "spec/.executor-*",
        "/spec-runner.config.yaml",
    )


def ensure_harness_excludes(repo_or_worktree: Path) -> None:
    """Idempotently add the maestro ignore block to the repo's exclude file.

    Writes to `$GIT_COMMON_DIR/info/exclude` — repo-local and shared by all
    linked worktrees (git resolves a linked worktree's `info/` to the common
    dir; a per-worktree exclude does not exist without worktreeConfig).
    `spec/.executor-*` is deliberately broader than the prefix: runtime
    state (incl. the unprefixed `.executor-stop`) must never be committed.

    Raises:
        WorkspaceError: If the path is not inside a git repository.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_or_worktree),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"cannot resolve git common dir for {repo_or_worktree}: "
            f"{result.stderr.strip()}"
        )
        raise WorkspaceError(msg)
    exclude = Path(result.stdout.strip()) / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if _EXCLUDE_BEGIN in existing:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    block = "\n".join([_EXCLUDE_BEGIN, *_exclude_patterns(), _EXCLUDE_END])
    with exclude.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(block + "\n")


class WorkspaceManager:
    """Manages isolated workspaces for workstreams via git worktrees.

    Each workstream gets its own worktree directory with its own
    branch, providing filesystem-level isolation for parallel
    spec-runner processes.
    """

    def __init__(
        self,
        git_manager: GitManager,
        workspace_base: Path,
    ) -> None:
        """Initialize workspace manager.

        Args:
            git_manager: GitManager for the main repository.
            workspace_base: Base directory for worktree dirs.
        """
        self._git = git_manager
        self._workspace_base = workspace_base

    @property
    def workspace_base(self) -> Path:
        """Return the base directory for workspaces."""
        return self._workspace_base

    def create_workspace(self, workstream_id: str, branch: str) -> Path:
        """Create an isolated workspace for a workstream.

        Creates a git worktree at {workspace_base}/{workstream_id}
        on a new branch.

        Args:
            workstream_id: Unique identifier for the workstream.
            branch: Git branch name for this workspace.

        Returns:
            Path to the created workspace directory.

        Raises:
            WorkspaceExistsError: If workspace dir exists.
            WorkspaceError: If worktree creation fails.
        """
        workspace_path = self._workspace_base / workstream_id

        if workspace_path.exists():
            msg = f"Workspace directory already exists: {workspace_path}"
            raise WorkspaceExistsError(msg)

        # Ensure base directory exists
        self._workspace_base.mkdir(parents=True, exist_ok=True)

        try:
            self._git.create_worktree(workspace_path, branch)
        except WorktreeError as e:
            msg = f"Failed to create workspace for '{workstream_id}': {e}"
            raise WorkspaceError(msg) from e

        return workspace_path

    def setup_spec_runner(
        self,
        workspace_path: Path,
        config: dict[str, Any],
    ) -> None:
        """Write spec-runner configuration into workspace.

        Creates executor.config.yaml and spec/ directory.

        Args:
            workspace_path: Path to the workspace directory.
            config: Spec-runner config dict for YAML output.

        Raises:
            WorkspaceNotFoundError: If workspace does not exist.
        """
        if not workspace_path.exists():
            msg = f"Workspace not found: {workspace_path}"
            raise WorkspaceNotFoundError(msg)

        # Ensure spec/ directory exists
        spec_dir = workspace_path / "spec"
        spec_dir.mkdir(exist_ok=True)

        # Clean stale spec-runner state from previous runs
        # (the worktree inherits spec/ from the base branch)
        for stale in [
            spec_dir / f".executor-{SPEC_PREFIX}state.db",
            spec_dir / f".executor-{SPEC_PREFIX}state.db-wal",
            spec_dir / f".executor-{SPEC_PREFIX}state.db-shm",
            spec_dir / f".executor-{SPEC_PREFIX}state.json",
            spec_dir / ".executor-state.db",
            spec_dir / ".executor-state.db-wal",
            spec_dir / ".executor-state.db-shm",
            spec_dir / ".executor-state.json",
            spec_dir / ".executor-state.lock",
            spec_dir / ".executor-progress.txt",
            spec_dir / ".task-history.log",
        ]:
            stale.unlink(missing_ok=True)

        # Write spec-runner.config.yaml in workspace root (v2.0 location)
        config_file = workspace_path / "spec-runner.config.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f, default_flow_style=False)

    def cleanup_workspace(self, workstream_id: str, force: bool = True) -> None:
        """Remove a workspace and its worktree.

        Args:
            workstream_id: Identifier of the workstream.
            force: Force removal even if dirty.

        Raises:
            WorkspaceError: If cleanup fails.
        """
        workspace_path = self._workspace_base / workstream_id

        if not workspace_path.exists():
            return  # Already cleaned up

        try:
            self._git.remove_worktree(workspace_path, force=force)
        except WorktreeError:
            # If git worktree remove fails, try manual cleanup
            shutil.rmtree(workspace_path, ignore_errors=True)
            self._git.prune_worktrees()

    def get_workspace_path(self, workstream_id: str) -> Path:
        """Get the workspace path for a workstream.

        Args:
            workstream_id: Workstream identifier.

        Returns:
            Path to the workspace directory.

        Raises:
            WorkspaceNotFoundError: If workspace not found.
        """
        workspace_path = self._workspace_base / workstream_id

        if not workspace_path.exists():
            msg = f"Workspace not found: {workspace_path}"
            raise WorkspaceNotFoundError(msg)

        return workspace_path

    def workspace_exists(self, workstream_id: str) -> bool:
        """Check if a workspace exists for a workstream."""
        return (self._workspace_base / workstream_id).exists()

    def list_workspaces(self) -> list[Path]:
        """List all workspace directories.

        Returns:
            List of paths to existing workspace directories.
        """
        if not self._workspace_base.exists():
            return []

        return [
            p
            for p in sorted(self._workspace_base.iterdir())
            if p.is_dir() and not p.name.startswith(".")
        ]

    def cleanup_all(self) -> None:
        """Remove all workspaces and their worktrees."""
        for workspace in self.list_workspaces():
            workstream_id = workspace.name
            self.cleanup_workspace(workstream_id, force=True)
