"""Tests for `maestro check-scope` (raw scope-containment check, exit 0/1/2).

Builds a real temp git repo (not mocks), following the pattern established
by tests/test_orchestrator_scope_gate.py: one repo, a base commit on `main`,
and a `feature` branch/commit checked out in the same directory (its own
worktree in spirit — `workspace_path` on the workstream row points at it).

DB setup runs through `asyncio.run` inside a sync fixture factory, matching
the `_setup_db_with_*` helpers in tests/test_cli.py — `CliRunner.invoke`
drives the (sync) Typer command, which itself calls `asyncio.run` under the
hood, so the test functions must stay sync (no nested event loop).
"""

import subprocess
from asyncio import run as asyncio_run
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maestro.cli import app
from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


runner = CliRunner()


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    """Init a git repo with one commit on its default branch (`main`)."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# test\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "initial")


def _commit_changes(repo: Path, branch: str, changed: list[str]) -> str:
    """Branch off HEAD, write+commit `changed` paths, return the new sha."""
    _run(repo, "checkout", "-b", branch)
    for rel in changed:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "workstream changes")
    return _run(repo, "rev-parse", "HEAD")


@pytest.fixture
def scope_check_repo(tmp_path):
    """Factory: build a repo+worktree+DB+workstream row, return (db_path, ws_id).

    `workspace_path` on the workstream row is the repo dir, checked out on
    `feature/<ws_id>` with `changed` committed on top of the `main` base.
    When `approve=True`, records an `(ex_post, <feature HEAD sha>)` gate
    approval via `approve_workstream_with_gate_record` (which requires the
    workstream to be NEEDS_REVIEW at call time).
    """

    def _build(
        *, scope: list[str], changed: list[str], approve: bool = False
    ) -> tuple[Path, str]:
        ws_id = "ws-1"
        repo = tmp_path / "repo"
        _init_repo(repo)
        sha = _commit_changes(repo, f"feature/{ws_id}", changed)
        db_path = tmp_path / "m.db"

        async def _setup() -> None:
            db = Database(db_path)
            await db.connect()
            try:
                status = (
                    WorkstreamStatus.NEEDS_REVIEW
                    if approve
                    else WorkstreamStatus.RUNNING
                )
                await db.create_workstream(
                    Workstream(
                        id=ws_id,
                        title=ws_id,
                        description="d",
                        scope=scope,
                        branch=f"feature/{ws_id}",
                        workspace_path=str(repo),
                        status=status,
                    )
                )
                if approve:
                    await db.approve_workstream_with_gate_record(ws_id, "ex_post", sha)
            finally:
                await db.close()

        asyncio_run(_setup())
        return db_path, ws_id

    return _build


def test_exit_2_on_unknown_workstream(tmp_path):
    db = tmp_path / "m.db"
    # empty/nonexistent DB row
    result = runner.invoke(
        app, ["check-scope", "nope", "--base", "main", "--db", str(db)]
    )
    assert result.exit_code == 2


def test_exit_1_on_escape(scope_check_repo):
    # scope_check_repo fixture: builds a temp repo+worktree, a maestro.db with a
    # workstream row (scope, workspace_path, branch) whose diff escapes scope.
    db, ws_id = scope_check_repo(scope=["src/**"], changed=["docs/x.md"])
    result = runner.invoke(
        app, ["check-scope", ws_id, "--base", "main", "--db", str(db)]
    )
    assert result.exit_code == 1
    assert "docs/x.md" in result.stdout


def test_exit_0_when_clean(scope_check_repo):
    db, ws_id = scope_check_repo(scope=["src/**"], changed=["src/ok.py"])
    result = runner.invoke(
        app, ["check-scope", ws_id, "--base", "main", "--db", str(db)]
    )
    assert result.exit_code == 0


def test_exit_0_on_empty_scope(scope_check_repo):
    db, ws_id = scope_check_repo(scope=[], changed=["anything.py"])
    result = runner.invoke(
        app, ["check-scope", ws_id, "--base", "main", "--db", str(db)]
    )
    assert result.exit_code == 0


def test_approval_prints_note_but_exit_stays_1(scope_check_repo):
    db, ws_id = scope_check_repo(scope=["src/**"], changed=["docs/x.md"], approve=True)
    result = runner.invoke(
        app, ["check-scope", ws_id, "--base", "main", "--db", str(db)]
    )
    assert result.exit_code == 1  # raw check ignores approval for the exit code
    assert "approved" in result.stdout.lower()
