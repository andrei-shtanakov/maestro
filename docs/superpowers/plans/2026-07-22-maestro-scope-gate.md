# Executable scope-gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce, as a deterministic always-on gate, that a workstream's own committed diff touches only paths matched by its declared `scope`; any escape blocks the merge with an operator override.

**Architecture:** A pure containment core (`find_escapes`) is fed by a git changed-paths source (`changed_paths_since`, branch-point diff). The orchestrator runs it at the ex-post edge before the optional steward gate; escapes route the workstream to `NEEDS_REVIEW` with a marker-bearing block reason that the existing `workstream-approve` + H-6 resume machinery can clear. A `maestro check-scope` CLI exposes the same containment check for operators/CI.

**Tech Stack:** Python 3.12+, pydantic, Typer CLI, `git` subprocess, pytest (pytest-asyncio auto mode), ruff, pyrefly.

## Global Constraints

- Package manager: `uv` only (`uv run pytest`, `uv add`). Never pip.
- Type hints on all code; `uv run pyrefly check` must report 0 errors.
- `uv run ruff format .` and `uv run ruff check .` must pass; line length 88.
- Async tests: `async def test_...` under pytest-asyncio auto mode (repo convention — no explicit `@pytest.mark.asyncio` / anyio markers needed).
- Follow existing patterns in `maestro/`. Public functions get docstrings.
- Approval marker format is fixed: `gates:approval-required phase=<ex_ante|ex_post> sha=<sha>` (`APPROVAL_MARKER_PREFIX`). Do not invent a second format.
- Spec: `docs/superpowers/specs/2026-07-22-maestro-scope-gate-design.md`.

---

## File Structure

- Create `maestro/scope_gate.py` — pure matcher: `normalize`, `_glob_to_regex`, `find_escapes`, `build_scope_escape_reason`.
- Create `maestro/gate_approvals.py` — neutral home for the approval-marker primitives moved out of `gates.py`, plus `build_approval_marker`.
- Create `maestro/changed_paths.py` — git changed-paths source: `changed_paths_since`, `_orchestrator_managed`, `_ORCHESTRATOR_MANAGED`.
- Modify `maestro/gates.py` — import/re-export marker primitives from `gate_approvals`; delegate ex-post diff to `changed_paths_since`; drop moved defs.
- Modify `maestro/orchestrator.py` — add `_gate_scope`, call it in `_handle_success` before `_gate_ex_post`.
- Modify `maestro/cli.py` — add the `check-scope` command.
- Tests: `tests/test_scope_gate.py`, `tests/test_gate_approvals.py`, `tests/test_changed_paths.py`, `tests/test_orchestrator_scope_gate.py`, `tests/test_cli_check_scope.py`.

---

## Task 1: Pure containment core (`scope_gate.py`)

**Files:**
- Create: `maestro/scope_gate.py`
- Test: `tests/test_scope_gate.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib `re` only).
- Produces:
  - `def normalize(paths: list[str]) -> list[str]` — strip leading `./`, backslash→slash.
  - `def find_escapes(changed_paths: list[str], scope: list[str]) -> list[str]` — subset of `changed_paths` matched by no pattern; `[]` when `scope` is empty. Assumes normalized input.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scope_gate.py
from maestro.scope_gate import find_escapes, normalize


def test_exact_match_is_in_scope():
    assert find_escapes(["src/foo.py"], ["src/foo.py"]) == []


def test_exact_pattern_does_not_match_other_file():
    assert find_escapes(["src/bar.py"], ["src/foo.py"]) == ["src/bar.py"]


def test_double_star_covers_nested():
    assert find_escapes(["src/a/b.py", "src/foo.py"], ["src/**"]) == []


def test_single_star_does_not_cross_slash():
    # '*.py' matches top-level only
    assert find_escapes(["a/foo.py"], ["*.py"]) == ["a/foo.py"]
    assert find_escapes(["foo.py"], ["*.py"]) == []


def test_dir_double_star_matches_contents_not_bare_dir():
    assert find_escapes(["dir/x.py"], ["dir/**"]) == []
    # a bare 'dir' path (no trailing slash) is NOT matched by 'dir/**'
    assert find_escapes(["dir"], ["dir/**"]) == ["dir"]


def test_leading_double_star():
    assert find_escapes(["a/b/foo.py", "foo.py"], ["**/foo.py"]) == []


def test_escape_in_parent_dir():
    assert find_escapes(["other/x.py"], ["src/**"]) == ["other/x.py"]


def test_empty_scope_skips():
    assert find_escapes(["anything.py"], []) == []


def test_deleted_path_string_still_matched():
    # find_escapes never touches the filesystem; a deleted path is just a string
    assert find_escapes(["src/gone.py"], ["src/**"]) == []


def test_multiple_patterns_union():
    assert find_escapes(
        ["src/a.py", "docs/b.md", "x/c"], ["src/**", "docs/**"]
    ) == ["x/c"]


def test_normalize_strips_dot_slash_and_backslash():
    assert normalize(["./src/a.py", "src\\b.py"]) == ["src/a.py", "src/b.py"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scope_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.scope_gate'`.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/scope_gate.py
"""Pure scope-containment matcher (no git, no FS, no DB).

`find_escapes` answers one question: which of these changed paths is matched
by none of the declared scope globs? Callers pass already-normalized,
repo-relative POSIX paths and patterns (see `normalize`).
"""

from __future__ import annotations

import re


def normalize(paths: list[str]) -> list[str]:
    """Normalize to repo-relative POSIX form: backslash->slash, strip './'."""
    result: list[str] = []
    for raw in paths:
        p = raw.replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        result.append(p)
    return result


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a pathlib-glob-style pattern to an anchored regex.

    `**` matches any number of segments (including zero); `*` matches within a
    single segment (never crosses `/`); `?` matches one non-slash char; every
    other character is literal.
    """
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:[^/]*/)*")  # '**/' -> zero+ leading dirs
                else:
                    out.append(".*")  # trailing '**' -> everything
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def find_escapes(changed_paths: list[str], scope: list[str]) -> list[str]:
    """Return the changed paths matched by no scope pattern.

    Empty result means containment holds. An empty `scope` returns `[]`
    (nothing to enforce — skip).
    """
    if not scope:
        return []
    matchers = [_glob_to_regex(p) for p in scope]
    return [
        path
        for path in changed_paths
        if not any(m.match(path) for m in matchers)
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scope_gate.py -q`
Expected: PASS (all 11 tests).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format maestro/scope_gate.py tests/test_scope_gate.py && uv run ruff check maestro/scope_gate.py tests/test_scope_gate.py && uv run pyrefly check`
Expected: formatted, all checks pass, 0 pyrefly errors.

- [ ] **Step 6: Commit**

```bash
git add maestro/scope_gate.py tests/test_scope_gate.py
git commit -m "feat(scope-gate): pure containment core (find_escapes)"
```

---

## Task 2: Approval-marker primitives → neutral module (`gate_approvals.py`)

Pure refactor. Moves the marker primitives out of `gates.py` so `scope_gate`/`changed_paths` can build markers without importing the heavy `gates` module. `gates.py` re-exports them, so every existing import site keeps working.

**Files:**
- Create: `maestro/gate_approvals.py`
- Modify: `maestro/gates.py` (remove moved defs; import + re-export from `gate_approvals`)
- Test: `tests/test_gate_approvals.py`

**Interfaces:**
- Consumes: nothing heavy (`re`, `pydantic`).
- Produces:
  - `APPROVAL_MARKER_PREFIX: str` = `"gates:approval-required"`
  - `BLOCK_REASON_PREFIX: str` = `"gates: human.owner_approval required"`
  - `class ApprovalMarker(BaseModel)` — frozen, `phase: Literal["ex_ante","ex_post"]`, `sha: str`
  - `def parse_approval_marker(error_message: str | None) -> ApprovalMarker | None`
  - `def preserve_approval_marker(new_message: str, prior: str | None) -> str`
  - `def build_approval_marker(phase: str, sha: str) -> str` — returns `f"{APPROVAL_MARKER_PREFIX} phase={phase} sha={sha}"` (NEW)

- [ ] **Step 1: Read the current primitives to move**

Read `maestro/gates.py` lines 51–125 (the `__all__` entries, `APPROVAL_MARKER_PREFIX`, `BLOCK_REASON_PREFIX`, `_MARKER_RE` / regex, `ApprovalMarker`, `parse_approval_marker`, `preserve_approval_marker`). Copy their exact bodies — do not rewrite them.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_gate_approvals.py
from maestro.gate_approvals import (
    ApprovalMarker,
    build_approval_marker,
    parse_approval_marker,
)


def test_build_then_parse_roundtrips():
    marker = build_approval_marker("ex_post", "abc123")
    assert marker == "gates:approval-required phase=ex_post sha=abc123"
    parsed = parse_approval_marker(f"scope escape: a.py; re-queue to approve. {marker}")
    assert parsed == ApprovalMarker(phase="ex_post", sha="abc123")


def test_parse_returns_none_without_marker():
    assert parse_approval_marker("scope escape: a.py") is None


def test_gates_still_reexports_primitives():
    # Backward-compat: existing import sites use maestro.gates
    from maestro.gates import APPROVAL_MARKER_PREFIX, parse_approval_marker as pg
    assert APPROVAL_MARKER_PREFIX == "gates:approval-required"
    assert pg("x gates:approval-required phase=ex_ante sha=deadbeef").phase == "ex_ante"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_gate_approvals.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.gate_approvals'`.

- [ ] **Step 4: Create `gate_approvals.py`**

Create `maestro/gate_approvals.py` with the exact moved bodies from Step 1, plus `build_approval_marker`:

```python
# maestro/gate_approvals.py
"""Approval-marker primitives (H-6 durable approval memory).

Moved out of `gates.py` so lightweight modules (scope_gate, changed_paths) can
build/parse the marker without importing the full gates runtime. `gates.py`
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict
from typing import Literal


APPROVAL_MARKER_PREFIX = "gates:approval-required"
BLOCK_REASON_PREFIX = "gates: human.owner_approval required"

_MARKER_RE = re.compile(
    re.escape(APPROVAL_MARKER_PREFIX)
    + r"\s+phase=(?P<phase>ex_ante|ex_post)\s+sha=(?P<sha>[0-9a-fA-F]+)"
)


class ApprovalMarker(BaseModel):
    """Parsed `gates:approval-required phase=<p> sha=<sha>` marker (H-6)."""

    model_config = ConfigDict(frozen=True)

    phase: Literal["ex_ante", "ex_post"]
    sha: str


def build_approval_marker(phase: str, sha: str) -> str:
    """Render the durable approval marker embedded in a block reason."""
    return f"{APPROVAL_MARKER_PREFIX} phase={phase} sha={sha}"


def parse_approval_marker(error_message: str | None) -> ApprovalMarker | None:
    """Extract the gates approval marker from a stored block reason."""
    if not error_message:
        return None
    match = _MARKER_RE.search(error_message)
    if match is None:
        return None
    return ApprovalMarker(phase=match.group("phase"), sha=match.group("sha"))


def preserve_approval_marker(new_message: str, prior: str | None) -> str:
    """Re-append the prior marker to a new message if it carried one."""
    marker = parse_approval_marker(prior)
    if marker is None:
        return new_message
    return f"{new_message} {build_approval_marker(marker.phase, marker.sha)}"
```

> NOTE: If the real `_MARKER_RE` or `parse_approval_marker`/`preserve_approval_marker` bodies read in Step 1 differ from the above (e.g. a different sha charset or preserve logic), use the EXACT original bodies. The only additions are the module docstring and `build_approval_marker`.

- [ ] **Step 5: Rewire `gates.py` to re-export**

In `maestro/gates.py`: delete the moved definitions (`APPROVAL_MARKER_PREFIX`, `BLOCK_REASON_PREFIX`, the marker regex, `ApprovalMarker`, `parse_approval_marker`, `preserve_approval_marker`). Add near the top imports:

```python
from maestro.gate_approvals import (
    APPROVAL_MARKER_PREFIX,
    ApprovalMarker,
    BLOCK_REASON_PREFIX,
    build_approval_marker,
    parse_approval_marker,
    preserve_approval_marker,
)
```

Keep the existing `__all__` entries (they now re-export the imported names). Replace the inline marker construction at the old line ~356 (`marker = f"{APPROVAL_MARKER_PREFIX} phase={phase} sha={sha}"`) with `marker = build_approval_marker(phase, sha)`.

- [ ] **Step 6: Run the moved-primitive + regression tests**

Run: `uv run pytest tests/test_gate_approvals.py tests/test_gates.py -q`
Expected: PASS (new tests pass; existing gate tests unchanged and green).

- [ ] **Step 7: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green (no import-site breakage in cli.py / orchestrator.py).

- [ ] **Step 8: Commit**

```bash
git add maestro/gate_approvals.py maestro/gates.py tests/test_gate_approvals.py
git commit -m "refactor(gates): move approval-marker primitives to gate_approvals; add build_approval_marker"
```

---

## Task 3: Changed-paths source (`changed_paths.py`)

**Files:**
- Create: `maestro/changed_paths.py`
- Test: `tests/test_changed_paths.py`

**Interfaces:**
- Consumes: `SPEC_PREFIX` from `maestro.models`.
- Produces:
  - `async def changed_paths_since(base_ref: str, head_ref: str, repo_root: Path) -> list[str]` — the workstream's OWN committed changes: `git merge-base base_ref head_ref`, then `git diff --no-renames -z --name-only <merge_base> <head_ref>`, orchestrator-managed artifacts filtered out, normalized.
  - `def _orchestrator_managed(path: str) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_changed_paths.py
import asyncio
from pathlib import Path

from maestro.changed_paths import changed_paths_since


async def _git(repo: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode()


async def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    await _git(repo, "init", "-b", "main")
    await _git(repo, "config", "user.email", "t@t.t")
    await _git(repo, "config", "user.name", "t")
    (repo / "base.py").write_text("x\n")
    await _git(repo, "add", ".")
    await _git(repo, "commit", "-m", "base")
    return repo


async def test_reports_added_and_deleted_paths(tmp_path):
    repo = await _init_repo(tmp_path)
    await _git(repo, "checkout", "-b", "feature")
    (repo / "src").mkdir()
    (repo / "src" / "new.py").write_text("y\n")
    (repo / "base.py").unlink()
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "work")
    paths = await changed_paths_since("main", "HEAD", repo)
    assert sorted(paths) == ["base.py", "src/new.py"]


async def test_branch_point_isolation_ignores_advanced_base(tmp_path):
    # base advances with an out-of-scope commit AFTER feature branched;
    # changed_paths_since must still report only feature's own change.
    repo = await _init_repo(tmp_path)
    await _git(repo, "checkout", "-b", "feature")
    (repo / "mine.py").write_text("f\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "feature work")
    await _git(repo, "checkout", "main")
    (repo / "sibling.py").write_text("s\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "sibling merged into base")
    paths = await changed_paths_since("main", "feature", repo)
    assert paths == ["mine.py"]  # NOT ['mine.py','sibling.py'] and no false escape


async def test_no_renames_under_hostile_config(tmp_path):
    repo = await _init_repo(tmp_path)
    await _git(repo, "config", "diff.renames", "true")
    await _git(repo, "checkout", "-b", "feature")
    (repo / "base.py").rename(repo / "renamed.py")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "rename")
    paths = await changed_paths_since("main", "HEAD", repo)
    assert sorted(paths) == ["base.py", "renamed.py"]  # delete + add, not a rename


async def test_orchestrator_managed_filtered(tmp_path):
    from maestro.models import SPEC_PREFIX
    repo = await _init_repo(tmp_path)
    await _git(repo, "checkout", "-b", "feature")
    (repo / "spec").mkdir()
    (repo / "spec" / f"{SPEC_PREFIX}tasks.md").write_text("t\n")
    (repo / "keep.py").write_text("k\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "work + harness")
    paths = await changed_paths_since("main", "HEAD", repo)
    assert paths == ["keep.py"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_changed_paths.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.changed_paths'`.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/changed_paths.py
"""Git changed-paths source for the scope-gate (Phase 0, local worktree).

Isolates the workstream's OWN committed changes from its branch-point, so a
sibling workstream advancing the base branch cannot inject false escapes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from maestro.models import SPEC_PREFIX
from maestro.scope_gate import normalize


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_changed_paths.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format maestro/changed_paths.py tests/test_changed_paths.py && uv run ruff check maestro/changed_paths.py tests/test_changed_paths.py && uv run pyrefly check`
Expected: clean, 0 errors.

- [ ] **Step 6: Commit**

```bash
git add maestro/changed_paths.py tests/test_changed_paths.py
git commit -m "feat(scope-gate): changed_paths_since (branch-point, --no-renames -z)"
```

---

## Task 4: GateKeeper delegates to `changed_paths_since`

Adopts the branch-point + `--no-renames` source inside the existing steward ex-post gate — a correctness fix (it had the same false-escape-after-base-advances bug) and one source of truth.

**Files:**
- Modify: `maestro/gates.py` (`evaluate_ex_post`; drop `_git_diff_paths`, `_ORCHESTRATOR_MANAGED`, `_orchestrator_managed`)
- Test: `tests/test_gates.py` (add one regression)

**Interfaces:**
- Consumes: `changed_paths_since` from `maestro.changed_paths` (Task 3).
- Produces: no new public surface; `GateKeeper.evaluate_ex_post` behavior shifts from two-dot to branch-point.

- [ ] **Step 1: Write the failing regression test**

```python
# tests/test_gates.py  (add to the existing file)
async def test_evaluate_ex_post_uses_branch_point_no_false_escape(tmp_path):
    """Base advanced by a sibling commit must not appear in ex-post diff paths."""
    import asyncio
    from pathlib import Path
    from maestro.changed_paths import changed_paths_since

    async def g(repo, *a):
        p = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo), *a,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        o, e = await p.communicate()
        assert p.returncode == 0, e.decode()
        return o.decode()

    repo = tmp_path / "r"; repo.mkdir()
    await g(repo, "init", "-b", "main")
    await g(repo, "config", "user.email", "t@t.t"); await g(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("1\n"); await g(repo, "add", "-A"); await g(repo, "commit", "-m", "0")
    await g(repo, "checkout", "-b", "feature")
    (repo / "mine.py").write_text("m\n"); await g(repo, "add", "-A"); await g(repo, "commit", "-m", "f")
    await g(repo, "checkout", "main")
    (repo / "sibling.py").write_text("s\n"); await g(repo, "add", "-A"); await g(repo, "commit", "-m", "s")
    assert await changed_paths_since("main", "feature", repo) == ["mine.py"]
```

- [ ] **Step 2: Run it — passes for the helper, but confirm GateKeeper still uses the old path**

Run: `uv run pytest tests/test_changed_paths.py -q` (already green) and read `gates.py::evaluate_ex_post` (~lines 239–260) to confirm it still calls `self._git_diff_paths` + `_orchestrator_managed`.

- [ ] **Step 3: Rewire `evaluate_ex_post`**

In `maestro/gates.py`, add import:

```python
from maestro.changed_paths import changed_paths_since
```

Replace the paths computation in `evaluate_ex_post` (the block that does
`[p for p in await self._git_diff_paths(workspace) if not _orchestrator_managed(p)]`) with:

```python
        paths = await changed_paths_since(self._base_branch, "HEAD", workspace)
```

Delete `_git_diff_paths` (method), and the module-level `_ORCHESTRATOR_MANAGED` tuple and `_orchestrator_managed` function (now owned by `changed_paths.py`). Keep `_git` / `_git_sha` (still used for sha). Remove any now-unused imports flagged by ruff.

- [ ] **Step 4: Run gate tests**

Run: `uv run pytest tests/test_gates.py -q`
Expected: PASS. If a pre-existing ex-post test asserted two-dot behavior with an advanced base, update its expectation to the branch-point result (document the change in the commit).

- [ ] **Step 5: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add maestro/gates.py tests/test_gates.py
git commit -m "fix(gates): ex-post diff via branch-point changed_paths_since (no false escape after base advances)"
```

---

## Task 5: Scope-escape block-reason builder (`scope_gate.py`)

The bridge that makes an escape approvable: a marker-bearing, path-truncated block reason.

**Files:**
- Modify: `maestro/scope_gate.py` (add `build_scope_escape_reason`)
- Test: `tests/test_scope_gate.py` (add cases)

**Interfaces:**
- Consumes: `build_approval_marker` from `maestro.gate_approvals` (Task 2).
- Produces: `def build_scope_escape_reason(escapes: list[str], sha: str, *, max_paths: int = 3) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scope_gate.py  (add)
from maestro.gate_approvals import parse_approval_marker
from maestro.scope_gate import build_scope_escape_reason


def test_reason_is_parseable_by_approval_marker():
    reason = build_scope_escape_reason(["a.py", "b.py"], "deadbeef")
    marker = parse_approval_marker(reason)
    assert marker is not None
    assert marker.phase == "ex_post"
    assert marker.sha == "deadbeef"


def test_reason_truncates_paths_but_keeps_marker():
    reason = build_scope_escape_reason(
        ["a.py", "b.py", "c.py", "d.py", "e.py"], "cafe1234", max_paths=3
    )
    assert "a.py, b.py, c.py" in reason
    assert "(+2 more)" in reason
    assert "d.py" not in reason
    # marker survives truncation intact
    assert parse_approval_marker(reason).sha == "cafe1234"


def test_reason_without_truncation_lists_all():
    reason = build_scope_escape_reason(["a.py"], "sha1")
    assert "(+0 more)" not in reason
    assert "more)" not in reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scope_gate.py -k reason -q`
Expected: FAIL — `ImportError: cannot import name 'build_scope_escape_reason'`.

- [ ] **Step 3: Implement**

```python
# maestro/scope_gate.py  (add import + function)
from maestro.gate_approvals import build_approval_marker


def build_scope_escape_reason(
    escapes: list[str], sha: str, *, max_paths: int = 3
) -> str:
    """Marker-bearing block reason for a scope escape.

    Truncates only the path list (never the marker), so
    `workstream-approve` can always record the `(ex_post, sha)` approval.
    """
    shown = escapes[:max_paths]
    listing = ", ".join(shown)
    if len(escapes) > max_paths:
        listing += f", ... (+{len(escapes) - max_paths} more)"
    marker = build_approval_marker("ex_post", sha)
    return f"scope escape: {listing}; re-queue to approve. {marker}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scope_gate.py -q`
Expected: PASS (all Task 1 + Task 5 tests).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format maestro/scope_gate.py tests/test_scope_gate.py && uv run ruff check maestro/scope_gate.py tests/test_scope_gate.py && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/scope_gate.py tests/test_scope_gate.py
git commit -m "feat(scope-gate): marker-bearing scope-escape block reason"
```

---

## Task 6: Orchestrator integration (`_gate_scope`)

**Files:**
- Modify: `maestro/orchestrator.py` (add `_gate_scope`; call it in `_handle_success` before `_gate_ex_post`)
- Test: `tests/test_orchestrator_scope_gate.py`

**Interfaces:**
- Consumes: `changed_paths_since` (Task 3), `find_escapes` + `normalize` + `build_scope_escape_reason` (Tasks 1, 5), `self._db.list_gate_approvals`, `self._workspace_head`, `self._config.base_branch`, `self._stats`, `WorkstreamStatus`.
- Produces: `async def _gate_scope(self, workstream_id: str, workstream: Workstream, workspace_path: Path) -> bool` — `True` = proceed, `False` = blocked (workstream routed to `NEEDS_REVIEW`).

- [ ] **Step 1: Write the failing test**

Model this on the existing `_gate_ex_post` tests in the repo (find them with `grep -rn "_gate_ex_post\|_handle_success" tests/`). Use the same orchestrator fixture/harness those use. Cases:

```python
# tests/test_orchestrator_scope_gate.py
# (Reuse the orchestrator construction helper already used by the existing
#  ex-post gate tests — import or replicate it. Pseudocode-free skeleton:)

import pytest
from pathlib import Path
from maestro.models import WorkstreamStatus


async def test_scope_escape_blocks_to_needs_review_with_marker(orchestrator_with_ws):
    orch, db, ws_id, worktree = orchestrator_with_ws(
        scope=["src/**"], changed=["src/a.py", "docs/evil.md"]
    )
    ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
    assert ok is False
    ws = await db.get_workstream(ws_id)
    assert ws.status == WorkstreamStatus.NEEDS_REVIEW
    assert "docs/evil.md" in ws.error_message
    assert "gates:approval-required phase=ex_post" in ws.error_message
    assert orch._stats.failed == 1
    assert worktree.exists()  # worktree intact


async def test_clean_workstream_passes(orchestrator_with_ws):
    orch, db, ws_id, worktree = orchestrator_with_ws(
        scope=["src/**"], changed=["src/a.py", "src/b.py"]
    )
    ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
    assert ok is True


async def test_empty_scope_skips(orchestrator_with_ws):
    orch, db, ws_id, worktree = orchestrator_with_ws(scope=[], changed=["anything.py"])
    ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
    assert ok is True


async def test_existing_approval_skips_without_diffing(orchestrator_with_ws, monkeypatch):
    orch, db, ws_id, worktree = orchestrator_with_ws(
        scope=["src/**"], changed=["docs/evil.md"]
    )
    head = await orch._workspace_head(worktree)
    await db.approve_workstream_with_gate_record(ws_id, "ex_post", head)
    called = {"diff": False}
    import maestro.orchestrator as om
    async def spy(*a, **k):
        called["diff"] = True
        return ["docs/evil.md"]
    monkeypatch.setattr(om, "changed_paths_since", spy)
    ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
    assert ok is True
    assert called["diff"] is False  # approval short-circuits before diff (§5.2)


async def test_approval_for_ws_a_does_not_waive_ws_b(orchestrator_with_two_ws):
    # Two workstreams whose worktrees sit at the same HEAD sha; approving A
    # must NOT let B's escape through (per-workstream approval namespace, §6).
    orch, db, a_id, b_id, wt_a, wt_b, head = orchestrator_with_two_ws(
        scope=["src/**"], changed=["docs/evil.md"]
    )
    await db.approve_workstream_with_gate_record(a_id, "ex_post", head)
    ok = await orch._gate_scope(b_id, await db.get_workstream(b_id), wt_b)
    assert ok is False  # B is still blocked
    assert (await db.get_workstream(b_id)).status == WorkstreamStatus.NEEDS_REVIEW
```

> `orchestrator_with_two_ws` builds two workstream rows whose worktrees are checked out at the same HEAD sha (e.g. two worktrees of the same feature commit). If constructing two same-sha worktrees is awkward in the harness, assert the same invariant directly against the DB: `approve_workstream_with_gate_record(a_id, "ex_post", head)` then `assert ("ex_post", head) not in await db.list_gate_approvals(b_id)`.

> If the existing ex-post gate tests build the orchestrator + a real temp git worktree via a fixture, reuse it and drop the `orchestrator_with_ws` shim. The assertions above are the contract; adapt construction to the repo's established test harness.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_scope_gate.py -q`
Expected: FAIL — `AttributeError: 'Orchestrator' object has no attribute '_gate_scope'`.

- [ ] **Step 3: Implement `_gate_scope` and wire it in**

Add imports at the top of `maestro/orchestrator.py`:

```python
from maestro.changed_paths import changed_paths_since
from maestro.scope_gate import build_scope_escape_reason, find_escapes, normalize
```

Add the method (near `_gate_ex_post`):

```python
    async def _gate_scope(
        self,
        workstream_id: str,
        workstream: Workstream,
        workspace_path: Path,
    ) -> bool:
        """Deterministic always-on scope containment (ex-post edge).

        The workstream's own committed diff must touch only paths matched by
        its declared scope. Escapes block RUNNING -> FAILED -> NEEDS_REVIEW
        with a marker-bearing reason. Empty scope skips.
        """
        scope = workstream.scope
        if not scope:
            return True
        head = await self._workspace_head(workspace_path)
        if head is None:
            reason = "scope-gate: cannot read worktree HEAD"
            self._logger.warning("%s for '%s'", reason, workstream_id)
            await self._db.update_workstream_status(
                workstream_id, WorkstreamStatus.FAILED,
                expected_status=WorkstreamStatus.RUNNING, error_message=reason)
            await self._db.update_workstream_status(
                workstream_id, WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED, error_message=reason)
            self._stats.failed += 1
            return False
        approvals = await self._db.list_gate_approvals(workstream_id)
        if ("ex_post", head) in approvals:
            return True
        paths = await changed_paths_since(
            self._config.base_branch, "HEAD", workspace_path
        )
        escapes = find_escapes(normalize(paths), normalize(scope))
        if not escapes:
            return True
        self._logger.warning(
            "scope escape in '%s' (%d paths): %s",
            workstream_id, len(escapes), escapes,  # FULL list to structured log
        )
        reason = build_scope_escape_reason(escapes, head)
        await self._db.update_workstream_status(
            workstream_id, WorkstreamStatus.FAILED,
            expected_status=WorkstreamStatus.RUNNING, error_message=reason)
        await self._db.update_workstream_status(
            workstream_id, WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.FAILED, error_message=reason)
        self._stats.failed += 1
        return False
```

Wire it into `_handle_success`, immediately before the existing `_gate_ex_post` call:

```python
        # Deterministic scope containment (always-on, before the optional
        # steward risk gate). Fail-fast on out-of-scope commits.
        if not await self._gate_scope(workstream_id, workstream, workspace_path):
            return

        # Gates (WS-006): ex-post guard over the actual diff ...
        if not await self._gate_ex_post(workstream_id, workstream, workspace_path):
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator_scope_gate.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the H-6 resume path**

Confirm the existing ex-post resume test suite still passes with the new gate inserted (an approved workstream must resume to DONE past `_gate_scope`):

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: PASS. If an existing success-path test now trips `_gate_scope` because its fixture commits out-of-scope files, give that fixture a scope that covers its changes (or `scope=[]`), and note it in the commit.

- [ ] **Step 6: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 7: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator_scope_gate.py
git commit -m "feat(scope-gate): always-on ex-post containment gate in orchestrator"
```

---

## Task 7: CLI `maestro check-scope`

Raw containment check for operators/CI. Needs `--base` because `base_branch` is not persisted on the workstream row.

**Files:**
- Modify: `maestro/cli.py` (add `check-scope` command)
- Test: `tests/test_cli_check_scope.py`

**Interfaces:**
- Consumes: `Database`, `changed_paths_since`, `find_escapes`, `normalize`, `list_gate_approvals`, `DEFAULT_DB_PATH`.
- Produces: CLI command `check-scope <workstream-id> --base <branch> [--db <path>]`. Exit `0` clean/skip, `1` escapes, `2` invalid input.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_check_scope.py
from typer.testing import CliRunner
from maestro.cli import app

runner = CliRunner()


def test_exit_2_on_unknown_workstream(tmp_path):
    db = tmp_path / "m.db"
    # empty/nonexistent DB row
    result = runner.invoke(app, ["check-scope", "nope", "--base", "main", "--db", str(db)])
    assert result.exit_code == 2


def test_exit_1_on_escape(scope_check_repo):
    # scope_check_repo fixture: builds a temp repo+worktree, a maestro.db with a
    # workstream row (scope, workspace_path, branch) whose diff escapes scope.
    db, ws_id = scope_check_repo(scope=["src/**"], changed=["docs/x.md"])
    result = runner.invoke(app, ["check-scope", ws_id, "--base", "main", "--db", str(db)])
    assert result.exit_code == 1
    assert "docs/x.md" in result.stdout


def test_exit_0_when_clean(scope_check_repo):
    db, ws_id = scope_check_repo(scope=["src/**"], changed=["src/ok.py"])
    result = runner.invoke(app, ["check-scope", ws_id, "--base", "main", "--db", str(db)])
    assert result.exit_code == 0


def test_exit_0_on_empty_scope(scope_check_repo):
    db, ws_id = scope_check_repo(scope=[], changed=["anything.py"])
    result = runner.invoke(app, ["check-scope", ws_id, "--base", "main", "--db", str(db)])
    assert result.exit_code == 0


def test_approval_prints_note_but_exit_stays_1(scope_check_repo):
    db, ws_id = scope_check_repo(scope=["src/**"], changed=["docs/x.md"], approve=True)
    result = runner.invoke(app, ["check-scope", ws_id, "--base", "main", "--db", str(db)])
    assert result.exit_code == 1  # raw check ignores approval for the exit code
    assert "approved" in result.stdout.lower()
```

> Build the `scope_check_repo` fixture in this test file: create a temp git repo with a `main` commit + a `feature` branch/worktree whose commit adds `changed`, insert a workstream row via `Database` (`add_workstream` / the repo's existing insert helper) with `scope`, `branch`, `workspace_path` set to the worktree; when `approve=True`, call `approve_workstream_with_gate_record(ws_id, "ex_post", <worktree HEAD>)`. Reuse row-construction helpers already used by `tests/test_database.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_check_scope.py -q`
Expected: FAIL — no such command `check-scope` (Typer exits 2 with usage error, but the escape/clean assertions fail).

- [ ] **Step 3: Implement the command**

Add to `maestro/cli.py`:

```python
@app.command("check-scope")
def check_scope_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to check")],
    base: Annotated[
        str, typer.Option("--base", "-b", help="Base branch to diff against")
    ],
    db: Annotated[
        Path | None, typer.Option("--db", "-d", help="Path to SQLite database file")
    ] = None,
) -> None:
    """Raw scope-containment check for a workstream's worktree.

    Exit 0 = clean or empty scope; 1 = escapes found; 2 = invalid input.
    An existing approval prints an informational note but never changes the
    exit code (this reports the containment fact, not the gate's policy).

    Examples:
        maestro check-scope my-ws --base main --db run/maestro.db
    """
    from maestro.changed_paths import changed_paths_since
    from maestro.database import Database
    from maestro.scope_gate import find_escapes, normalize

    db_path = db or DEFAULT_DB_PATH

    async def _run() -> int:
        database = Database(db_path)
        await database.connect()
        try:
            ws = await database.get_workstream(workstream_id)
            if ws is None:
                console.print(f"[red]workstream '{workstream_id}' not found[/red]")
                return 2
            if not ws.workspace_path:
                console.print(f"[red]workstream '{workstream_id}' has no worktree[/red]")
                return 2
            worktree = Path(ws.workspace_path)
            if not worktree.exists():
                console.print(f"[red]worktree missing: {worktree}[/red]")
                return 2
            if not ws.scope:
                console.print("[dim]empty scope — nothing to enforce.[/dim]")
                return 0
            try:
                paths = await changed_paths_since(base, "HEAD", worktree)
            except RuntimeError as exc:
                console.print(f"[red]git error: {exc}[/red]")
                return 2
            escapes = find_escapes(normalize(paths), normalize(ws.scope))
            if not escapes:
                console.print("[green]in scope — no escapes.[/green]")
                return 0
            console.print("[red]scope escape:[/red]")
            for p in escapes:
                console.print(f"  {p}")
            # Raw check: an existing ex_post approval is informational only and
            # does NOT change the exit code (spec §7). Any recorded ex_post
            # approval for this workstream is enough to print the note.
            approvals = await database.list_gate_approvals(workstream_id)
            for phase, sha in approvals:
                if phase == "ex_post":
                    console.print(f"[dim]note: approved (ex_post, {sha[:12]})[/dim]")
                    break
            return 1
        finally:
            await database.close()

    raise typer.Exit(asyncio.run(_run()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_check_scope.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 6: Update docs**

Add the `check-scope` line to `CLAUDE.md` under the Mode-2 CLI section:

```
uv run maestro check-scope <workstream-id> --base <base-branch> --db maestro.db  # deterministic scope containment (exit 1 on escape)
```

- [ ] **Step 7: Commit**

```bash
git add maestro/cli.py tests/test_cli_check_scope.py CLAUDE.md
git commit -m "feat(scope-gate): maestro check-scope CLI (raw containment, exit 0/1/2)"
```

---

## Task 8: Verification & PR

- [ ] **Step 1: Full green gate**

Run: `uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check`
Expected: all pass, 0 pyrefly errors.

- [ ] **Step 2: Manual smoke of the CLI**

Build a throwaway repo+worktree+db (or reuse a dogfood run), then:
Run: `uv run maestro check-scope <ws> --base <base> --db <db>`
Expected: exit 1 and the escaping paths printed when the worktree has an out-of-scope commit; exit 0 when clean.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/scope-gate
gh pr create --title "feat: executable scope-gate (idea #7a)" --body "<summary + link to spec docs/superpowers/specs/2026-07-22-maestro-scope-gate-design.md>"
```

- [ ] **Step 4: Read Copilot review**

Address valid inline comments with follow-up commits; reply with rationale to invalid ones. Do not merge (the user merges).

---

## Self-Review (plan vs spec)

- **§2 goal / §3.1 core** → Task 1 (`find_escapes`, `normalize`).
- **§3.2 source, branch-point, `--no-renames -z`** → Task 3 (`changed_paths_since`) + regression tests.
- **§3.2 GateKeeper delegates (correctness fix)** → Task 4.
- **§4 glob semantics** → Task 1 tests (exact / `**` / `*` no-cross / `dir/**` contents / empty skip / deleted-string).
- **§4 renames = delete+add** → Task 3 `test_no_renames_under_hostile_config`.
- **§5 always-on, before steward, FAILED→NEEDS_REVIEW, worktree intact, stats.failed** → Task 6.
- **§5.1 marker-bearing reason, truncate paths only, shared marker helpers, full list to logger** → Task 2 (`gate_approvals`, `build_approval_marker`) + Task 5 (`build_scope_escape_reason`) + Task 6 (logger).
- **§5.2 approval-before-diff** → Task 6 `test_existing_approval_skips_without_diffing`.
- **§6 override reuse, per-workstream namespace, marker dependency** → Task 6 (approval check) + Task 5 (marker) + covered by marker round-trip in Task 6 escape test.
- **§7 CLI raw, exit 0/1/2, approval note** → Task 7.
- **§8 no config** → nothing to build (verified: no new config field added).
- **§9 tests** → distributed across Tasks 1,3,4,5,6,7; per-workstream approval isolation is an explicit case in Task 6 (`test_approval_for_ws_a_does_not_waive_ws_b`), with a DB-level fallback assertion documented.
- **§10 out of scope** → no remote source, no `--json`, no uncommitted enforcement, no #7b: none appear in tasks. ✓
