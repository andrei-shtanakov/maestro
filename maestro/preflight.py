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
    """Check that repo exists and is a git repository.

    Args:
        repo: Path to the repository.

    Returns:
        List of errors if repo is missing or not a git repository.
    """
    if not repo.exists():
        return [
            ValidationIssue(
                severity="error",
                code="repo-missing",
                message=(
                    f"repo_path does not exist: {repo}. Fix repo_path in the config."
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

    Args:
        repo: Path to the repository root.
        patterns: List of glob patterns (may include ./).

    Returns:
        Dict mapping each pattern to the set of matching file paths
        (relative to repo).
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
    """Check for non-matching glob patterns and exact scope overlaps.

    The static heuristic (at parse time) catches obvious overlaps like
    'src/**' vs 'src/auth/**'. This tier catches misses like './src/**'
    vs 'src/**' by expanding every pattern against actual files.

    Args:
        workstreams: List of workstream configs to check.
        repo: Path to the repository root.
        seen_pairs: Pairs already reported by the static tier
            (to avoid duplicates).

    Returns:
        List of warnings for unmatched patterns and overlapping scopes.
    """
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
