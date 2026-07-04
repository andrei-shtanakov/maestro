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
