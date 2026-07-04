"""Tests for the maestro init scaffold generator."""

import subprocess
from pathlib import Path

import yaml

from maestro.models import OrchestratorConfig
from maestro.scaffold import generate_project_yaml


def make_git_repo(tmp_path: Path, *, remote: str | None) -> Path:
    repo = tmp_path / "myproject"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    if remote:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    return repo


def load_generated(content: str) -> OrchestratorConfig:
    """Every generated config must satisfy the pydantic schema."""
    return OrchestratorConfig(**yaml.safe_load(content))


class TestGenerateProjectYaml:
    def test_git_repo_with_remote(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, remote="https://github.com/user/myproject")
        content = generate_project_yaml(repo)
        config = load_generated(content)
        assert config.project == "myproject"
        assert config.repo_url == "https://github.com/user/myproject"
        assert config.repo_path == str(repo)
        assert config.workspace_base == "/tmp/maestro-ws/myproject"
        assert len(config.workstreams) == 1

    def test_git_repo_without_remote_uses_placeholder(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, remote=None)
        content = generate_project_yaml(repo)
        # Placeholder must still pass the schema (review item #4):
        # non-empty repo_url, absolute repo_path.
        config = load_generated(content)
        assert "TODO" in content
        assert config.repo_url  # non-empty placeholder

    def test_non_git_cwd_still_generates_schema_valid_config(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        content = generate_project_yaml(plain)
        config = load_generated(content)  # schema passes; FS checks would fail
        assert config.repo_path == str(plain)

    def test_project_name_override(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, remote=None)
        content = generate_project_yaml(repo, project="custom-name")
        config = load_generated(content)
        assert config.project == "custom-name"
        assert config.workspace_base == "/tmp/maestro-ws/custom-name"

    def test_base_branch_detected_from_current_branch(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, remote=None)
        content = generate_project_yaml(repo)
        config = load_generated(content)
        # no origin/HEAD in a fresh repo -> falls back to current branch
        assert config.base_branch == "main"
