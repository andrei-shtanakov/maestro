"""Golden drift test: a real `spec-runner plan --full` produces a tasks.md
that spec-runner's own parser accepts. Auto-skipped without spec-runner;
runs as a weekly CI job (plan --full spends real Claude tokens)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from maestro.decomposer import ProjectDecomposer
from maestro.models import WorkstreamConfig


pytestmark = pytest.mark.skipif(
    shutil.which("spec-runner") is None, reason="spec-runner not installed"
)


@pytest.mark.anyio
@pytest.mark.slow
async def test_plan_full_produces_parseable_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)  # noqa: ASYNC221
    ws = WorkstreamConfig(
        id="golden",
        title="Add a greeting helper",
        description="Add a function that returns a greeting string, with a test.",
        scope=["src/greet.py", "tests/test_greet.py"],
    )
    dec = ProjectDecomposer(repo_path=workspace, spec_gen_budget_usd=2.0)
    await dec.generate_spec(ws, workspace)

    tasks = workspace / "spec" / "tasks.md"
    assert tasks.is_file()
    # spec-runner's own parser accepts the generated tasks.md. Note:
    # `--project-root` is a top-level spec-runner flag, not a `task list`
    # subcommand flag — it must precede the subcommand.
    result = subprocess.run(  # noqa: ASYNC221
        ["spec-runner", "--project-root", str(workspace), "task", "list"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
