"""Git changed-paths source for the scope-gate (Phase 0, local worktree).

Isolates the workstream's OWN committed changes from its branch-point, so a
sibling workstream advancing the base branch cannot inject false escapes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from maestro.models import SPEC_PREFIX
from maestro.scope_gate import normalize


if TYPE_CHECKING:
    from pathlib import Path


_ORCHESTRATOR_MANAGED = (
    f"spec/{SPEC_PREFIX}",
    f"spec/.{SPEC_PREFIX}",
    "spec/.executor-",
)


def _orchestrator_managed(path: str) -> bool:
    """True for harness artifacts that never count as workstream changes."""
    return path.startswith(_ORCHESTRATOR_MANAGED)


async def _run_git(repo_root: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_root),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        msg = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
    return stdout.decode()


async def changed_paths_since(
    base_ref: str, head_ref: str, repo_root: Path
) -> list[str]:
    """Repo-relative POSIX paths the workstream changed since its branch-point.

    merge_base = `git merge-base base_ref head_ref`
    paths      = `git diff --no-renames -z --name-only <merge_base> <head_ref>`

    `--no-renames` forces delete+add even under `diff.renames=true`; `-z`
    NUL-splits for filename robustness. Orchestrator-managed artifacts are
    dropped.
    """
    merge_base = (await _run_git(repo_root, "merge-base", base_ref, head_ref)).strip()
    raw = await _run_git(
        repo_root, "diff", "--no-renames", "-z", "--name-only", merge_base, head_ref
    )
    paths = [p for p in raw.split("\0") if p]
    return [p for p in normalize(paths) if not _orchestrator_managed(p)]
