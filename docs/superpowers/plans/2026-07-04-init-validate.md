# `maestro init` + `maestro validate` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `maestro validate` (preflight checks for Mode-2 project.yaml: cycles, scope overlap, filesystem sanity) and `maestro init` (non-interactive scaffold with git-derived autofill), with the same validation running automatically inside `maestro orchestrate`.

**Architecture:** New pure-logic module `maestro/preflight.py` returns a `ValidationReport` (errors + warnings); new `maestro/scaffold.py` generates a commented project.yaml template; `cli.py` gains two thin Typer commands and wires preflight into `_run_orchestrator`. Cycle detection is extracted from `DAG` into a shared pure function so there is exactly one Kahn implementation.

**Tech Stack:** Python 3.12+, pydantic, Typer + Rich, pytest (anyio auto mode), `typer.testing.CliRunner`.

**Spec:** `docs/superpowers/specs/2026-07-04-init-validate-design.md`

## Global Constraints

- Package management: `uv` only (`uv run pytest`, `uv run pyrefly check`, `uv run ruff format .`, `uv run ruff check .`).
- Type hints on all code; public APIs get docstrings; line length 88.
- After every task: `uv run pytest` green, `uv run pyrefly check` clean, `uv run ruff format . && uv run ruff check .` clean.
- `orchestrator.py` must NOT be modified (programmatic API unchanged).
- Schema-level validation (duplicate IDs, unknown deps, self-dep) stays in pydantic models — preflight must NOT re-implement it.
- `_git_query` in `scaffold.py` is private and must not be imported elsewhere.
- Commit after each task with a conventional-commit message ending in the Co-Authored-By trailer used in this repo.

---

### Task 1: Extract shared `find_cycle` pure function in `dag.py`

Behavior-preserving refactor: `DAG._detect_cycles` / `DAG._find_cycle_path`
(`maestro/dag.py:109-181`) become a module-level pure function. Existing DAG
tests must pass unchanged — they prove the extraction preserves behavior.

**Files:**
- Modify: `maestro/dag.py` (replace `_detect_cycles` body, delete `_find_cycle_path`, add `find_cycle` + `_cycle_path` module-level functions)
- Test: `tests/test_dag.py` (append a new test class)

**Interfaces:**
- Produces: `find_cycle(deps: dict[str, set[str]]) -> list[str] | None` in `maestro/dag.py` — returns a cycle path like `["a", "b", "a"]` (first node repeated at the end) or `None` if acyclic. Dependencies pointing at ids absent from `deps` keys are ignored (matches `DAG._build_graph`, which only adds edges for known nodes).

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dag.py`:

```python
from maestro.dag import find_cycle


class TestFindCycle:
    """Tests for the shared pure cycle detector."""

    def test_no_cycle(self) -> None:
        deps = {"a": set(), "b": {"a"}, "c": {"b"}}
        assert find_cycle(deps) is None

    def test_empty_graph(self) -> None:
        assert find_cycle({}) is None

    def test_two_node_cycle(self) -> None:
        cycle = find_cycle({"a": {"b"}, "b": {"a"}})
        assert cycle is not None
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"a", "b"}

    def test_three_node_cycle(self) -> None:
        cycle = find_cycle({"a": {"c"}, "b": {"a"}, "c": {"b"}})
        assert cycle is not None
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"a", "b", "c"}

    def test_cycle_in_disconnected_component(self) -> None:
        deps = {"a": set(), "x": {"y"}, "y": {"x"}}
        cycle = find_cycle(deps)
        assert cycle is not None
        assert set(cycle) == {"x", "y"}

    def test_unknown_deps_ignored(self) -> None:
        # "ghost" is not a key -> edge ignored, same as DAG._build_graph
        assert find_cycle({"a": {"ghost"}, "b": {"a"}}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag.py::TestFindCycle -v`
Expected: FAIL — `ImportError: cannot import name 'find_cycle'`

- [ ] **Step 3: Implement `find_cycle` and refactor `DAG` to use it**

In `maestro/dag.py`, add after the `CycleError` class (module level):

```python
def find_cycle(deps: dict[str, set[str]]) -> list[str] | None:
    """Find a dependency cycle in an id -> dependencies mapping.

    Detects a cycle with Kahn's algorithm, then recovers the cycle path
    with DFS. Dependencies whose ids are not keys of ``deps`` are ignored.

    Args:
        deps: Mapping of node id to the set of ids it depends on.

    Returns:
        Cycle path with the first node repeated at the end
        (e.g. ``["a", "b", "a"]``), or None if the graph is acyclic.
    """
    known = set(deps)
    in_degree = {node: len(deps[node] & known) for node in deps}
    dependents: dict[str, set[str]] = {node: set() for node in deps}
    for node, node_deps in deps.items():
        for dep in node_deps & known:
            dependents[dep].add(node)

    queue: deque[str] = deque(
        node for node, degree in in_degree.items() if degree == 0
    )
    processed = 0
    while queue:
        node = queue.popleft()
        processed += 1
        for dependent in dependents[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if processed == len(deps):
        return None
    return _cycle_path(deps, known)


def _cycle_path(deps: dict[str, set[str]], known: set[str]) -> list[str]:
    """Recover one cycle path via DFS (called only when a cycle exists)."""
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def dfs(node: str, path: list[str]) -> list[str] | None:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        for dep in deps[node] & known:
            if dep not in visited:
                result = dfs(dep, path)
                if result:
                    return result
            elif dep in rec_stack:
                cycle_start = path.index(dep)
                return [*path[cycle_start:], dep]
        path.pop()
        rec_stack.remove(node)
        return None

    for node in deps:
        if node not in visited:
            result = dfs(node, [])
            if result:
                return result
    return []
```

Replace the whole body of `DAG._detect_cycles` (keep the method, it is called
from `__init__`) and DELETE `DAG._find_cycle_path` entirely:

```python
    def _detect_cycles(self) -> None:
        """Detect cycles via the shared find_cycle function.

        Raises:
            CycleError: If a cycle is detected.
        """
        cycle = find_cycle(
            {node_id: node.dependencies for node_id, node in self._nodes.items()}
        )
        if cycle is not None:
            raise CycleError(cycle)
```

`from collections import deque` is already imported in `dag.py`.

- [ ] **Step 4: Run the full DAG test file**

Run: `uv run pytest tests/test_dag.py -v`
Expected: ALL PASS — new `TestFindCycle` tests plus every pre-existing DAG
test unchanged (this is the behavior-preservation proof).

- [ ] **Step 5: Quality gates**

Run: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/dag.py tests/test_dag.py
git commit -m "refactor(dag): extract shared find_cycle pure function

One Kahn implementation for both DAG and the upcoming preflight
validator (ADR: init+validate design, review item #1).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `maestro/preflight.py` — report models + static checks

**Files:**
- Create: `maestro/preflight.py`
- Test: `tests/test_preflight.py` (new)

**Interfaces:**
- Consumes: `find_cycle` from Task 1; `ProjectDecomposer.validate_non_overlap(workstreams) -> list[ScopeOverlapWarning]` (existing, `maestro/decomposer.py:400`; warning objects have `.workstream_a`, `.workstream_b`, `str()` renders the pattern list); `OrchestratorConfig` / `WorkstreamConfig` from `maestro.models`.
- Produces (used by Tasks 3-5):
  - `ValidationIssue(severity: Literal["error","warning"], code: str, workstream_ids: list[str] = [], message: str)`
  - `ValidationReport(issues: list[ValidationIssue])` with properties `errors`, `warnings`, `ok`
  - `validate_project(config: OrchestratorConfig, *, check_fs: bool = True) -> ValidationReport`

- [ ] **Step 1: Write failing tests**

Create `tests/test_preflight.py`:

```python
"""Unit tests for preflight validation (maestro validate)."""

from maestro.models import OrchestratorConfig, WorkstreamConfig
from maestro.preflight import ValidationIssue, ValidationReport, validate_project


def make_config(
    workstreams: list[WorkstreamConfig], repo_path: str = "/nonexistent"
) -> OrchestratorConfig:
    return OrchestratorConfig(
        project="test",
        repo_url="https://github.com/user/test",
        repo_path=repo_path,
        workspace_base="/tmp/maestro-ws/test",
        workstreams=workstreams,
    )


def ws(id_: str, scope: list[str], depends_on: list[str]) -> WorkstreamConfig:
    return WorkstreamConfig(
        id=id_,
        title=id_,
        description=f"workstream {id_}",
        scope=scope,
        depends_on=depends_on,
    )


class TestValidationReport:
    def test_ok_when_only_warnings(self) -> None:
        report = ValidationReport(
            issues=[
                ValidationIssue(
                    severity="warning", code="scope-empty", message="w"
                )
            ]
        )
        assert report.ok
        assert len(report.warnings) == 1
        assert report.errors == []

    def test_not_ok_with_errors(self) -> None:
        report = ValidationReport(
            issues=[
                ValidationIssue(severity="error", code="dag-cycle", message="e")
            ]
        )
        assert not report.ok
        assert len(report.errors) == 1


class TestStaticChecks:
    def test_clean_config_no_issues(self) -> None:
        config = make_config(
            [
                ws("a", ["src/a/**"], []),
                ws("b", ["src/b/**"], ["a"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert report.ok
        assert report.issues == []

    def test_two_node_cycle_is_error(self) -> None:
        config = make_config(
            [
                ws("a", ["src/a/**"], ["b"]),
                ws("b", ["src/b/**"], ["a"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert not report.ok
        codes = [i.code for i in report.errors]
        assert codes == ["dag-cycle"]
        assert set(report.errors[0].workstream_ids) == {"a", "b"}

    def test_three_node_cycle_is_error(self) -> None:
        config = make_config(
            [
                ws("a", ["src/a/**"], ["c"]),
                ws("b", ["src/b/**"], ["a"]),
                ws("c", ["src/c/**"], ["b"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert [i.code for i in report.errors] == ["dag-cycle"]

    def test_scope_overlap_is_warning(self) -> None:
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("b", ["src/auth/**"], []),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert report.ok  # warnings only
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert set(overlap[0].workstream_ids) == {"a", "b"}

    def test_empty_scope_is_warning(self) -> None:
        config = make_config([ws("a", [], [])])
        report = validate_project(config, check_fs=False)
        assert report.ok
        assert [i.code for i in report.issues] == ["scope-empty"]
        assert report.issues[0].workstream_ids == ["a"]

    def test_empty_workstreams_skips_dag_and_scope_checks(self) -> None:
        config = make_config([])
        report = validate_project(config, check_fs=False)
        assert report.ok
        assert report.issues == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.preflight'`

- [ ] **Step 3: Implement `maestro/preflight.py` (static part)**

```python
"""Preflight validation for Mode-2 orchestrator configs (project.yaml).

Aggregates errors and warnings into a ValidationReport instead of raising,
so callers can render everything at once. Schema-level validation (duplicate
ids, unknown deps, self-deps) stays in the pydantic models and is NOT
re-implemented here.

Used by the `maestro validate` CLI command and by `maestro orchestrate`
as a fail-fast preflight.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from maestro.dag import find_cycle
from maestro.decomposer import ProjectDecomposer
from maestro.models import OrchestratorConfig, WorkstreamConfig

Severity = Literal["error", "warning"]


class ValidationIssue(BaseModel):
    """A single preflight finding with a stable machine-readable code."""

    severity: Severity
    code: str
    workstream_ids: list[str] = Field(default_factory=list)
    message: str


class ValidationReport(BaseModel):
    """Aggregated preflight findings."""

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Issues that must block a run."""
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Issues that should be reviewed but do not block."""
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """True when no errors are present (warnings allowed)."""
        return not self.errors


def validate_project(
    config: OrchestratorConfig, *, check_fs: bool = True
) -> ValidationReport:
    """Run preflight checks over an orchestrator config.

    Args:
        config: Already schema-validated orchestrator config.
        check_fs: Also run filesystem checks (repo existence, glob
            matching, exact scope-overlap tier). Disable for
            deterministic runs without the real repo.

    Returns:
        Report with all findings; never raises for config-content problems.
    """
    issues: list[ValidationIssue] = []
    overlap_pairs: set[frozenset[str]] = set()

    if config.workstreams:
        issues.extend(_check_cycles(config.workstreams))
        issues.extend(_check_scope_empty(config.workstreams))
        issues.extend(_check_overlap_static(config.workstreams, overlap_pairs))

    if check_fs:
        repo = Path(config.repo_path).expanduser()
        repo_issues = _check_repo(repo)
        issues.extend(repo_issues)
        if not repo_issues and config.workstreams:
            issues.extend(_check_scope_fs(config.workstreams, repo, overlap_pairs))

    return ValidationReport(issues=issues)


def _check_cycles(workstreams: list[WorkstreamConfig]) -> list[ValidationIssue]:
    cycle = find_cycle({w.id: set(w.depends_on) for w in workstreams})
    if cycle is None:
        return []
    return [
        ValidationIssue(
            severity="error",
            code="dag-cycle",
            workstream_ids=cycle[:-1],
            message=(
                "Cyclic dependency between workstreams: "
                + " -> ".join(cycle)
                + ". Remove one of the depends_on edges to break the cycle."
            ),
        )
    ]


def _check_scope_empty(
    workstreams: list[WorkstreamConfig],
) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            severity="warning",
            code="scope-empty",
            workstream_ids=[w.id],
            message=(
                f"Workstream '{w.id}' has an empty scope: overlap checks "
                "cannot protect it from conflicts with parallel workstreams."
            ),
        )
        for w in workstreams
        if not w.scope
    ]


def _check_overlap_static(
    workstreams: list[WorkstreamConfig],
    seen_pairs: set[frozenset[str]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for warning in ProjectDecomposer.validate_non_overlap(workstreams):
        pair = frozenset({warning.workstream_a, warning.workstream_b})
        seen_pairs.add(pair)
        issues.append(
            ValidationIssue(
                severity="warning",
                code="scope-overlap",
                workstream_ids=sorted(pair),
                message=(
                    f"{warning}. Overlapping scopes risk merge conflicts; "
                    "split the scopes or add a depends_on edge."
                ),
            )
        )
    return issues


def _check_repo(repo: Path) -> list[ValidationIssue]:
    """Placeholder — implemented in the filesystem-checks task."""
    return []


def _check_scope_fs(
    workstreams: list[WorkstreamConfig],
    repo: Path,
    seen_pairs: set[frozenset[str]],
) -> list[ValidationIssue]:
    """Placeholder — implemented in the filesystem-checks task."""
    return []
```

Note: `_check_repo` / `_check_scope_fs` are stubs on purpose — Task 3
implements them test-first. The static tests all pass `check_fs=False`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: PASS (all).

- [ ] **Step 5: Quality gates + full suite**

Run: `uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: clean, no regressions.

- [ ] **Step 6: Commit**

```bash
git add maestro/preflight.py tests/test_preflight.py
git commit -m "feat(preflight): ValidationReport + static checks (cycles, overlap, empty scope)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Preflight filesystem checks (repo sanity, glob matching, exact overlap tier)

**Files:**
- Modify: `maestro/preflight.py` (implement `_check_repo`, `_check_scope_fs`, add `_expand_scope`)
- Test: `tests/test_preflight.py` (append)

**Interfaces:**
- Consumes: `validate_project` / stubs from Task 2.
- Produces: filesystem issue codes `repo-missing`, `repo-not-git`, `scope-no-match`, plus the exact tier of `scope-overlap` (de-duplicated against the static tier via `seen_pairs`).

- [ ] **Step 1: Write failing tests**

Append to `tests/test_preflight.py`:

```python
from pathlib import Path


def make_git_repo(tmp_path: Path, files: list[str]) -> Path:
    """Create a fake git repo with the given relative files."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for rel in files:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return repo


class TestFilesystemChecks:
    def test_repo_missing_is_error(self, tmp_path: Path) -> None:
        config = make_config(
            [ws("a", ["src/**"], [])], repo_path=str(tmp_path / "nope")
        )
        report = validate_project(config)
        assert [i.code for i in report.errors] == ["repo-missing"]

    def test_repo_not_git_is_error(self, tmp_path: Path) -> None:
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        config = make_config([ws("a", ["src/**"], [])], repo_path=str(plain_dir))
        report = validate_project(config)
        assert [i.code for i in report.errors] == ["repo-not-git"]

    def test_repo_errors_skip_scope_fs_checks(self, tmp_path: Path) -> None:
        config = make_config(
            [ws("a", ["src/**"], [])], repo_path=str(tmp_path / "nope")
        )
        report = validate_project(config)
        assert [i.code for i in report.issues] == ["repo-missing"]

    def test_glob_with_matches_is_silent(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", ["src/a/**"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert report.issues == []

    def test_glob_without_matches_is_warning(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config(
            [ws("a", ["src/a/**", "src/typo/**"], [])], repo_path=str(repo)
        )
        report = validate_project(config)
        assert [i.code for i in report.issues] == ["scope-no-match"]
        assert "src/typo/**" in report.issues[0].message

    def test_directory_scope_without_glob_counts_files(
        self, tmp_path: Path
    ) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", ["src/a"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert report.issues == []

    def test_check_fs_false_skips_everything(self, tmp_path: Path) -> None:
        config = make_config(
            [ws("a", ["src/**"], [])], repo_path=str(tmp_path / "nope")
        )
        report = validate_project(config, check_fs=False)
        assert report.issues == []


class TestExactOverlapTier:
    def test_heuristic_false_negative_caught_by_fs_tier(
        self, tmp_path: Path
    ) -> None:
        # './src/**' vs 'src/**' — the static heuristic misses this
        # (different first segment), the exact tier must catch it.
        repo = make_git_repo(tmp_path, ["src/main.py"])
        config = make_config(
            [
                ws("a", ["./src/**"], []),
                ws("b", ["src/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert set(overlap[0].workstream_ids) == {"a", "b"}
        assert "src/main.py" in overlap[0].message

    def test_no_duplicate_when_both_tiers_fire(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/auth/login.py"])
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("b", ["src/auth/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1  # static tier fired; exact tier de-duplicated

    def test_disjoint_scopes_no_overlap(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/x.py", "src/b/y.py"])
        config = make_config(
            [
                ws("a", ["src/a/**"], []),
                ws("b", ["src/b/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        assert report.issues == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preflight.py::TestFilesystemChecks tests/test_preflight.py::TestExactOverlapTier -v`
Expected: FAIL — stubs return `[]`, so `repo-missing`/`scope-no-match`/exact-tier assertions fail.

- [ ] **Step 3: Implement the filesystem checks**

Replace the two stubs in `maestro/preflight.py` and add `_expand_scope`:

```python
def _check_repo(repo: Path) -> list[ValidationIssue]:
    if not repo.exists():
        return [
            ValidationIssue(
                severity="error",
                code="repo-missing",
                message=(
                    f"repo_path does not exist: {repo}. "
                    "Fix repo_path in the config."
                ),
            )
        ]
    if not (repo / ".git").exists():
        return [
            ValidationIssue(
                severity="error",
                code="repo-not-git",
                message=(
                    f"repo_path is not a git repository (no .git): {repo}. "
                    "Point repo_path at a cloned repository."
                ),
            )
        ]
    return []


def _expand_scope(repo: Path, patterns: list[str]) -> dict[str, set[str]]:
    """Expand each glob to the set of matching repo-relative file paths.

    A pattern matching a directory counts every file under it. Files
    inside .git are excluded. Patterns are normalized by stripping a
    leading './' so './src/**' and 'src/**' expand identically.
    """
    matches: dict[str, set[str]] = {}
    for pattern in patterns:
        normalized = pattern.removeprefix("./")
        matched: set[str] = set()
        for p in repo.glob(normalized):
            rel = p.relative_to(repo)
            if ".git" in rel.parts:
                continue
            if p.is_file():
                matched.add(str(rel))
            elif p.is_dir():
                matched.update(
                    str(f.relative_to(repo))
                    for f in p.rglob("*")
                    if f.is_file() and ".git" not in f.relative_to(repo).parts
                )
        matches[pattern] = matched
    return matches


def _check_scope_fs(
    workstreams: list[WorkstreamConfig],
    repo: Path,
    seen_pairs: set[frozenset[str]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    files_by_ws: dict[str, set[str]] = {}

    for w in workstreams:
        expanded = _expand_scope(repo, w.scope)
        files_by_ws[w.id] = set().union(*expanded.values()) if expanded else set()
        for pattern, matched in expanded.items():
            if not matched:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="scope-no-match",
                        workstream_ids=[w.id],
                        message=(
                            f"Scope pattern '{pattern}' of workstream "
                            f"'{w.id}' matches no existing files — either a "
                            "typo or a glob for files not yet created."
                        ),
                    )
                )

    for i, a in enumerate(workstreams):
        for b in workstreams[i + 1 :]:
            pair = frozenset({a.id, b.id})
            if pair in seen_pairs:
                continue
            common = files_by_ws[a.id] & files_by_ws[b.id]
            if common:
                sample = ", ".join(sorted(common)[:5])
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="scope-overlap",
                        workstream_ids=sorted(pair),
                        message=(
                            f"Scopes of '{a.id}' and '{b.id}' match the same "
                            f"existing files (e.g. {sample}). Overlapping "
                            "scopes risk merge conflicts; split the scopes "
                            "or add a depends_on edge."
                        ),
                    )
                )
    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: PASS (all, including Task 2 tests).

- [ ] **Step 5: Quality gates + full suite**

Run: `uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/preflight.py tests/test_preflight.py
git commit -m "feat(preflight): filesystem checks + exact scope-overlap tier

repo-missing/repo-not-git errors, scope-no-match warning, and exact
file-set intersection that catches heuristic false negatives
(review item #2).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `maestro validate` CLI command

**Files:**
- Modify: `maestro/cli.py` (import preflight, add `_print_validation_report` helper + `validate` command; place the helper near the other `_display_*` helpers and the command after `approve_command`)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `validate_project`, `ValidationIssue`, `ValidationReport` (Task 2/3); `load_orchestrator_config` (`maestro/config.py:227`, raises `ConfigError` — already imported in cli.py).
- Produces: `_print_validation_report(report: ValidationReport) -> None` (reused by Task 5); CLI exit codes: 0 = no errors, 1 = errors, or warnings under `--strict`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cli.py` (uses the module-level `runner = CliRunner()` and `app` already imported there):

```python
class TestValidateCommand:
    """Tests for maestro validate."""

    @staticmethod
    def _write_project_yaml(
        tmp_path: Path, repo_path: Path, workstreams_yaml: str
    ) -> Path:
        config_file = tmp_path / "project.yaml"
        config_file.write_text(
            f"""
project: test
repo_url: https://github.com/user/test
repo_path: {repo_path}
workspace_base: /tmp/maestro-ws/test
workstreams:
{workstreams_yaml}
"""
        )
        return config_file

    @staticmethod
    def _make_repo(tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "a").mkdir(parents=True)
        (repo / "src" / "a" / "main.py").write_text("x")
        return repo

    def test_valid_config_exit_zero(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 0
        assert "0 errors, 0 warnings" in result.output

    def test_cycle_exit_one(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
    depends_on: [b]
  - id: b
    title: B
    description: d
    scope: ["src/b/**"]
    depends_on: [a]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 1
        assert "dag-cycle" in result.output

    def test_warnings_exit_zero_without_strict(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: []
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 0
        assert "scope-empty" in result.output

    def test_warnings_exit_one_with_strict(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: []
""",
        )
        result = runner.invoke(app, ["validate", str(config_file), "--strict"])
        assert result.exit_code == 1

    def test_no_fs_skips_repo_checks(self, tmp_path: Path) -> None:
        config_file = self._write_project_yaml(
            tmp_path,
            tmp_path / "missing-repo",
            """  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file), "--no-fs"])
        assert result.exit_code == 0

    def test_schema_error_exit_one(self, tmp_path: Path) -> None:
        config_file = tmp_path / "project.yaml"
        config_file.write_text("project: test\n")  # missing required fields
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 1
```

`test_cli.py` already imports `Path` and defines `runner`; do not re-import.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::TestValidateCommand -v`
Expected: FAIL — `No such command 'validate'` (exit code 2 from Typer).

- [ ] **Step 3: Implement the command**

In `maestro/cli.py`, add to imports:

```python
from maestro.preflight import ValidationIssue, ValidationReport, validate_project
```

Add the renderer next to the other display helpers (after `_display_summary`):

```python
def _print_validation_report(report: ValidationReport) -> None:
    """Render preflight issues and a summary line."""
    for issue in report.issues:
        color = "red" if issue.severity == "error" else "yellow"
        location = (
            f" {', '.join(issue.workstream_ids)}:" if issue.workstream_ids else ""
        )
        console.print(
            f"[{color}]{issue.severity}[/{color}] "
            f"\\[{issue.code}]{location} {issue.message}"
        )
    n_err, n_warn = len(report.errors), len(report.warnings)
    style = "red" if n_err else ("yellow" if n_warn else "green")
    console.print(f"[{style}]{n_err} errors, {n_warn} warnings[/{style}]")
```

Add the command after `approve_command`:

```python
@app.command("validate")
def validate_command(
    config: Annotated[
        Path,
        typer.Argument(
            help="Path to project YAML configuration",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Treat warnings as errors (exit 1)"),
    ] = False,
    no_fs: Annotated[
        bool,
        typer.Option(
            "--no-fs",
            help=(
                "Skip filesystem checks (repo existence, glob matching). "
                "Only the static overlap heuristic runs; it can miss "
                "overlaps the filesystem tier would catch."
            ),
        ),
    ] = False,
) -> None:
    """Validate a Mode-2 project.yaml without running it.

    Checks dependency cycles, scope overlaps, and repository sanity.
    Exit code 0 when there are no errors (warnings allowed unless
    --strict), 1 otherwise.
    """
    try:
        project = load_orchestrator_config(config)
    except ConfigError as e:
        _print_validation_report(
            ValidationReport(
                issues=[
                    ValidationIssue(
                        severity="error", code="schema", message=str(e)
                    )
                ]
            )
        )
        raise typer.Exit(1) from e

    report = validate_project(project, check_fs=not no_fs)
    _print_validation_report(report)
    if not report.ok or (strict and report.warnings):
        raise typer.Exit(1)
```

Deviation from the spec, intentional: the spec suggested "one issue per
pydantic error"; `load_orchestrator_config` already formats all pydantic
errors into one `ConfigError` message, so we emit a single `schema` issue
carrying that formatted text instead of re-parsing it — the loader stays the
single source of schema-error formatting.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py::TestValidateCommand -v`
Expected: PASS.

- [ ] **Step 5: Quality gates + full suite**

Run: `uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/cli.py tests/test_cli.py
git commit -m "feat(cli): maestro validate command with --strict and --no-fs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Preflight inside `maestro orchestrate`

**Files:**
- Modify: `maestro/cli.py` (`_run_orchestrator`, directly after the `load_orchestrator_config` try/except at cli.py:897-901)
- Test: `tests/test_cli.py` (append to the existing orchestrator-CLI test class area)

**Interfaces:**
- Consumes: `validate_project`, `_print_validation_report` (Task 4).
- Produces: `maestro orchestrate` exits 1 before any DB/orchestrator work when preflight finds errors; warnings print and the run continues.

- [ ] **Step 1: Write failing test**

Append to `tests/test_cli.py`:

```python
class TestOrchestratePreflight:
    """Preflight validation gates maestro orchestrate."""

    def test_orchestrate_aborts_on_cycle(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        config_file = tmp_path / "project.yaml"
        config_file.write_text(
            f"""
project: test
repo_url: https://github.com/user/test
repo_path: {repo}
workspace_base: {tmp_path / "ws"}
workstreams:
  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
    depends_on: [b]
  - id: b
    title: B
    description: d
    scope: ["src/b/**"]
    depends_on: [a]
"""
        )
        db_path = tmp_path / "maestro.db"
        result = runner.invoke(
            app, ["orchestrate", str(config_file), "--db", str(db_path)]
        )
        assert result.exit_code == 1
        assert "dag-cycle" in result.output
        # Aborted before any orchestrator work: no database was created
        assert not db_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestOrchestratePreflight -v`
Expected: FAIL — without the gate, orchestrate proceeds (creates the DB and
fails later or hangs; the `db_path.exists()` assertion breaks).

- [ ] **Step 3: Implement the gate**

In `maestro/cli.py::_run_orchestrator`, insert directly after the existing
`except ConfigError` block (after cli.py:901), before `db_path.parent.mkdir`:

```python
    report = validate_project(config)
    if report.issues:
        _print_validation_report(report)
    if not report.ok:
        err_console.print(
            "[red]Preflight validation failed.[/red] "
            f"Run 'maestro validate {config_path}' for details."
        )
        raise typer.Exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS — including the pre-existing orchestrate tests
(`test_run_orchestrator_clears_state_without_resume` etc. use valid configs;
if any of their fixture configs now trip an FS error — e.g. repo_path not a
git repo — fix the FIXTURE to be a real minimal repo (`(repo / ".git").mkdir`),
not the gate: the gate is the intended behavior.)

- [ ] **Step 5: Quality gates + full suite**

Run: `uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/cli.py tests/test_cli.py
git commit -m "feat(cli): fail-fast preflight validation in maestro orchestrate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: `maestro/scaffold.py` + `maestro init` CLI command

**Files:**
- Create: `maestro/scaffold.py`
- Modify: `maestro/cli.py` (add `init` command after `validate_command`)
- Test: `tests/test_scaffold.py` (new), `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `OrchestratorConfig` (schema self-check), `yaml.safe_load`.
- Produces: `generate_project_yaml(cwd: Path, project: str | None = None) -> str` and `ScaffoldError(Exception)` in `maestro/scaffold.py`; CLI `maestro init [PATH] [--force] [--project NAME]`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_scaffold.py`:

```python
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
        cwd=repo, check=True, capture_output=True,
    )
    if remote:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=repo, check=True, capture_output=True,
        )
    return repo


def load_generated(content: str) -> OrchestratorConfig:
    """Every generated config must satisfy the pydantic schema."""
    return OrchestratorConfig(**yaml.safe_load(content))


class TestGenerateProjectYaml:
    def test_git_repo_with_remote(self, tmp_path: Path) -> None:
        repo = make_git_repo(
            tmp_path, remote="https://github.com/user/myproject"
        )
        content = generate_project_yaml(repo)
        config = load_generated(content)
        assert config.project == "myproject"
        assert config.repo_url == "https://github.com/user/myproject"
        assert config.repo_path == str(repo)
        assert config.workspace_base == "/tmp/maestro-ws/myproject"
        assert len(config.workstreams) == 1

    def test_git_repo_without_remote_uses_placeholder(
        self, tmp_path: Path
    ) -> None:
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

    def test_base_branch_detected_from_current_branch(
        self, tmp_path: Path
    ) -> None:
        repo = make_git_repo(tmp_path, remote=None)
        content = generate_project_yaml(repo)
        config = load_generated(content)
        # no origin/HEAD in a fresh repo -> falls back to current branch
        assert config.base_branch == "main"
```

Append to `tests/test_cli.py`:

```python
class TestInitCommand:
    """Tests for maestro init."""

    def test_init_writes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / "project.yaml").exists()

    def test_init_refuses_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "project.yaml").write_text("existing")
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert (tmp_path / "project.yaml").read_text() == "existing"

    def test_init_force_overwrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "project.yaml").write_text("existing")
        result = runner.invoke(app, ["init", "--force"])
        assert result.exit_code == 0
        assert (tmp_path / "project.yaml").read_text() != "existing"
```

(`pytest` is already imported in `tests/test_cli.py`; check and add the import
only if missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scaffold.py tests/test_cli.py::TestInitCommand -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.scaffold'` and `No such command 'init'`.

- [ ] **Step 3: Implement `maestro/scaffold.py`**

```python
"""Scaffold generator for Mode-2 project.yaml configs (`maestro init`).

Generates a commented YAML template with values autofilled from the git
environment. The generated content is self-checked against the pydantic
schema before being returned, so `maestro init` can never emit a config
the loader rejects (this also catches template drift when the schema
evolves).
"""

import subprocess
from pathlib import Path
from string import Template

import yaml
from pydantic import ValidationError

from maestro.models import OrchestratorConfig


class ScaffoldError(Exception):
    """Raised when the generated config fails its schema self-check."""


REPO_URL_PLACEHOLDER = "https://github.com/OWNER/REPO"

_TEMPLATE = Template("""\
# Maestro Multi-Process Orchestrator configuration.
# Generated by `maestro init`. Full reference: examples/project.yaml
# in the Maestro repository.
#
# Usage:
#   maestro validate project.yaml
#   maestro orchestrate project.yaml

# --- Project metadata ---

project: $project

# Natural-language description of the project.
# If the `workstreams` section is removed, Maestro uses this description
# to auto-decompose the project via Claude CLI.
description: |
  TODO: describe what should be built.

# --- Repository settings ---

# GitHub remote URL (used for PR creation via gh CLI)
repo_url: $repo_url$repo_url_comment

# Local path to the repository (absolute or ~)
repo_path: $repo_path

# Base directory where git worktrees are created
workspace_base: $workspace_base

# --- Execution settings ---

max_concurrent: 3
base_branch: $base_branch
branch_prefix: "feature/"
auto_pr: true

# --- Workstreams ---
# Each workstream runs in an isolated git worktree on its own branch.
# `scope` globs declare file ownership; keep scopes non-overlapping so
# parallel workstreams cannot conflict. `depends_on` orders execution.

workstreams:
  - id: example-workstream
    title: "Example workstream"
    description: |
      TODO: describe this work unit.
    scope:
      - "src/example/**"
    depends_on: []
    priority: 0

# --- Notifications (optional) ---
# notifications:
#   desktop: true
""")


def _git_query(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git query; None when git/repo/value is unavailable.

    Private to scaffold: the rest of Maestro talks to git through the
    async, worktree-oriented GitManager. Do not import this elsewhere;
    if a third sync-git caller appears, promote it deliberately.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _detect_base_branch(cwd: Path) -> str:
    """origin default branch -> current branch -> 'main'."""
    ref = _git_query(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd
    )
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    return _git_query(["branch", "--show-current"], cwd) or "main"


def generate_project_yaml(cwd: Path, project: str | None = None) -> str:
    """Generate project.yaml content for the given directory.

    Args:
        cwd: Directory the config describes (usually the repo root).
        project: Project name override; defaults to the directory name.

    Returns:
        YAML text that is guaranteed to pass OrchestratorConfig.

    Raises:
        ScaffoldError: If the generated content fails the schema
            self-check (template drift bug — never expected at runtime).
    """
    name = project or cwd.name
    repo_url = _git_query(["remote", "get-url", "origin"], cwd)

    content = _TEMPLATE.substitute(
        project=name,
        repo_url=repo_url or REPO_URL_PLACEHOLDER,
        repo_url_comment=(
            "" if repo_url else "  # TODO: no origin remote found — fill in"
        ),
        repo_path=str(cwd.resolve()),
        workspace_base=f"/tmp/maestro-ws/{name}",
        base_branch=_detect_base_branch(cwd),
    )

    try:
        OrchestratorConfig(**yaml.safe_load(content))
    except (yaml.YAMLError, ValidationError) as exc:
        msg = (
            "internal error: generated config does not satisfy the "
            f"OrchestratorConfig schema: {exc}"
        )
        raise ScaffoldError(msg) from exc

    return content
```

- [ ] **Step 4: Implement the `init` command**

In `maestro/cli.py`, add to imports:

```python
from maestro.scaffold import ScaffoldError, generate_project_yaml
```

Add after `validate_command`:

```python
@app.command("init")
def init_command(
    path: Annotated[
        Path,
        typer.Argument(help="Output path for the generated config"),
    ] = Path("project.yaml"),
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite an existing file"),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option(
            "--project", help="Project name (default: current directory name)"
        ),
    ] = None,
) -> None:
    """Generate a Mode-2 project.yaml scaffold for the current directory.

    Values are autofilled from the git environment (remote URL, base
    branch); everything else gets commented, schema-valid defaults.
    """
    if path.exists() and not force:
        err_console.print(
            f"[red]{path} already exists.[/red] Use --force to overwrite."
        )
        raise typer.Exit(1)

    try:
        content = generate_project_yaml(Path.cwd(), project=project)
    except ScaffoldError as e:
        err_console.print(f"[red]Scaffold error:[/red] {e}")
        raise typer.Exit(1) from e

    path.write_text(content, encoding="utf-8")
    console.print(
        f"[green]Wrote {path}.[/green] Next: edit the workstreams, "
        f"then run 'maestro validate {path}'."
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_scaffold.py tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Quality gates + full suite**

Run: `uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add maestro/scaffold.py maestro/cli.py tests/test_scaffold.py tests/test_cli.py
git commit -m "feat(cli): maestro init — project.yaml scaffold with git autofill

Generated output is self-checked against OrchestratorConfig, so init
can never emit a config the loader rejects.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation sync + final verification

**Files:**
- Modify: `README.md` (CLI commands / examples area), `CLAUDE.md` (Development Commands + core modules list), `CHANGELOG.md` if present (check with `ls CHANGELOG.md`)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-6.
- Produces: docs describing `maestro init` / `maestro validate`.

- [ ] **Step 1: Update CLAUDE.md**

In the `## Development Commands` block, after the orchestrator commands, add:

```bash
# === Mode-2 config authoring ===
uv run maestro init                          # Scaffold project.yaml from cwd
uv run maestro validate project.yaml         # Preflight: cycles, scope overlap, repo sanity
uv run maestro validate project.yaml --strict --no-fs  # CI mode, no filesystem access
```

In the `### Core modules in maestro/` shared-infrastructure list, add (keep
alphabetical-ish placement near related modules):

```markdown
- **preflight.py**: Mode-2 config validation — ValidationReport (errors/warnings), cycle detection via shared dag.find_cycle, two-tier scope-overlap (static heuristic + exact file-set intersection), repo/glob filesystem checks; runs standalone (`maestro validate`) and as a fail-fast gate inside `maestro orchestrate`
- **scaffold.py**: `maestro init` generator — commented project.yaml template with git-derived autofill, self-checked against OrchestratorConfig before writing
```

- [ ] **Step 2: Update README.md**

Find the CLI/commands section (`grep -n "maestro orchestrate" README.md`) and
add `maestro init` + `maestro validate` with one-line descriptions and the
`--strict` / `--no-fs` flags, mirroring the CLAUDE.md wording. Add a CHANGELOG
entry under Unreleased if `CHANGELOG.md` exists.

- [ ] **Step 3: Full verification**

Run: `uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: full suite green, no type or lint issues.

Manual smoke (from the Maestro repo root, which is a git repo):

```bash
cd "$(mktemp -d)" && git init -q . && uv run --project /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro maestro init && uv run --project /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro maestro validate project.yaml
```

Expected: `Wrote project.yaml.`, then a `scope-no-match` warning (the example
workstream's `src/example/**` matches nothing in an empty repo) and exit 0.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md CHANGELOG.md
git commit -m "docs: document maestro init and maestro validate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
