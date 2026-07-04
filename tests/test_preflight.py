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
                ValidationIssue(severity="warning", code="scope-empty", message="w")
            ]
        )
        assert report.ok
        assert len(report.warnings) == 1
        assert report.errors == []

    def test_not_ok_with_errors(self) -> None:
        report = ValidationReport(
            issues=[ValidationIssue(severity="error", code="dag-cycle", message="e")]
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
