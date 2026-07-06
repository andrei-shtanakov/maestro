"""Golden drift test: a real `spec-runner plan --full` produces a tasks.md
that spec-runner's own parser accepts. Auto-skipped without spec-runner;
runs as a weekly CI job (plan --full spends real Claude tokens)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from maestro.decomposer import ProjectDecomposer
from maestro.models import WorkstreamConfig


# Real-cost opt-in guard. `spec-runner` is a normal PyPI tool present on many
# dev boxes, so `shutil.which` alone would let a bare `uv run pytest` fire a
# real `plan --full` and spend Claude tokens. Mirror the repo's env-var
# opt-in pattern for real-cost/real-subprocess tests (cf. MAESTRO_ARBITER_BIN
# gating the arbiter real-subprocess suite): require an explicit opt-in AND
# spec-runner present. The CI weekly job sets MAESTRO_RUN_GOLDEN=1.
pytestmark = pytest.mark.skipif(
    os.environ.get("MAESTRO_RUN_GOLDEN") != "1" or shutil.which("spec-runner") is None,
    reason="golden drift test is opt-in: set MAESTRO_RUN_GOLDEN=1 "
    "(spends real Claude tokens) and install spec-runner",
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
