"""Gates runtime (WS-006 handoff M-1..M-3): GateKeeper, verdict records, fail-closed."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path  # noqa: TC003

import pytest

from maestro.gates import (
    APPROVAL_MARKER_PREFIX,
    GateKeeper,
    pipeline_log_dir,
)
from maestro.models import GatesConfig, OrchestratorConfig, WorkstreamStatus


_SHA_RE = r"[0-9a-f]{40}"

# A stand-in steward binary: implements just enough of `risk-classify` for the
# tests — tier comes from the TIER file next to it; scope_violation is computed
# from facts.json exactly like the real classifier would flag it.
_FAKE_STEWARD = """#!/usr/bin/env python3
import json, pathlib, sys

args = sys.argv[1:]
assert args[0] == "risk-classify", args
here = pathlib.Path(__file__).parent
tier = (here / "TIER").read_text().strip()

def arg(flag):
    return args[args.index(flag) + 1] if flag in args else None

flags = []
phase = "ex_ante"
sha = "0" * 40
if arg("--declared"):
    data = json.loads(pathlib.Path(arg("--declared")).read_text())
    sha = data["sha"]
elif arg("--no-fs"):
    phase = "ex_post"
    data = json.loads(pathlib.Path(arg("--no-fs")).read_text())
    sha = data["sha"]
    declared = data.get("declared_scope")
    if declared is not None:
        import fnmatch
        for p in data["paths"]:
            if not any(fnmatch.fnmatch(p, g) or p.startswith(g.rstrip("*/")) for g in declared):
                flags.append("scope_violation")
                tier = "high"
                break
else:
    sys.exit(2)

gates = {
    "low": [],
    "medium": ["steward.gate_check"],
    "high": ["steward.gate_check", "maestro.validate_strict",
             "human.owner_approval", "git.required_reviews"],
    "critical": ["steward.gate_check", "maestro.validate_strict",
                 "human.owner_approval", "git.required_reviews",
                 "human.transition_approval"],
}[tier]
print(json.dumps({
    "tier": tier, "phase": phase,
    "inputs": {"change_class": "code", "blast_radius": "single-repo",
               "trust_boundary": "none"},
    "dominant_axis": "change_class", "floor_profile": "lite",
    "mandatory_gates": gates, "flags": flags, "sha": sha,
    "risk_model_version": "sha256:" + "f" * 64,
}))
"""


@pytest.fixture()
def fake_steward(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    steward = bin_dir / "steward"
    steward.write_text(_FAKE_STEWARD)
    steward.chmod(steward.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "TIER").write_text("low")
    return steward


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit on master."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
        )

    git("init", "-b", "master")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-m", "base")
    return repo


def _keeper(fake_steward: Path, repo: Path, tmp_path: Path) -> GateKeeper:
    gates = GatesConfig(steward_bin=str(fake_steward))
    return GateKeeper(
        gates,
        project="demo",
        repo_path=repo,
        base_branch="master",
        log_dir=tmp_path / "logs" / "01TEST",
    )


def _set_tier(fake_steward: Path, tier: str) -> None:
    (fake_steward.parent / "TIER").write_text(tier)


# ---------------------------------------------------------------- ex-ante


async def test_ex_ante_low_tier_allows_and_writes_records(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert decision.allow
    gate_ids = [r.gate_id for r in decision.records]
    assert "steward.risk_classify_ex_ante" in gate_ids
    jsonl = tmp_path / "logs" / "01TEST" / "gate_verdicts.jsonl"
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert lines and lines[0]["risk_model_version"].startswith("sha256:")
    assert all(len(rec["sha"]) == 40 for rec in lines if rec.get("sha"))


async def test_ex_ante_high_tier_blocks_for_approval(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not decision.allow
    assert decision.reason and APPROVAL_MARKER_PREFIX in decision.reason
    approvals = [r for r in decision.records if r.gate_id == "human.owner_approval"]
    assert approvals and approvals[0].verdict == "missing"
    assert approvals[0].obligation == "mandatory"


async def test_ex_ante_operator_requeue_counts_as_approval(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # Blocked once -> operator flips NEEDS_REVIEW -> READY; the stored reason
    # (same phase, same sha) is the approval memory.
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not first.allow
    second = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=first.reason)
    assert second.allow
    approvals = [r for r in second.records if r.gate_id == "human.owner_approval"]
    assert approvals and approvals[0].verdict == "pass"


async def test_ex_ante_approval_invalidated_by_new_sha(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # M-3: a new commit invalidates the stored approval (marker carries the sha).
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (repo / "src" / "a.py").write_text("x = 2\n")
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(repo), "commit", "-am", "more"],
        check=True,
        capture_output=True,
        env=env,
    )
    second = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=first.reason)
    assert not second.allow


async def test_missing_binary_fails_closed(repo: Path, tmp_path: Path) -> None:
    gates = GatesConfig(steward_bin=str(tmp_path / "nope"))
    keeper = GateKeeper(
        gates,
        project="demo",
        repo_path=repo,
        base_branch="master",
        log_dir=tmp_path / "logs" / "01TEST",
    )
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not decision.allow
    errs = [r for r in decision.records if r.verdict == "error"]
    assert errs and errs[0].obligation == "mandatory"


async def test_steward_failure_fails_closed(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    fake_steward.write_text("#!/bin/sh\nexit 2\n")  # noqa: ASYNC240
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not decision.allow
    assert any(r.verdict == "error" for r in decision.records)


# ---------------------------------------------------------------- ex-post


async def test_ex_post_within_scope_allows(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    workspace = _branch_with_change(repo, "src/a.py", "x = 3\n")
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, prior_error=None
    )
    assert decision.allow


async def test_ex_post_scope_violation_blocks(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    workspace = _branch_with_change(repo, "rogue.txt", "outside\n")
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, prior_error=None
    )
    assert not decision.allow


def _branch_with_change(repo: Path, rel: str, content: str) -> Path:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
        )

    git("switch", "-c", "feature/ws-1")
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git("add", "-A")
    git("commit", "-m", "change")
    return repo


# ---------------------------------------------------------------- config + state machine


def test_gates_config_parses_from_yaml() -> None:
    cfg = OrchestratorConfig.model_validate(
        {
            "project": "demo",
            "repo_url": "https://example.com/x",
            "repo_path": "/tmp/x",
            "workspace_base": "/tmp/ws",
            "workstreams": [],
            "gates": {"steward_bin": "/usr/local/bin/steward", "profile": "team"},
        }
    )
    assert cfg.gates is not None
    assert cfg.gates.profile == "team"
    assert cfg.gates.mode == "fail_closed"


def test_gates_absent_by_default() -> None:
    cfg = OrchestratorConfig(
        project="demo",
        repo_url="https://example.com/x",
        repo_path="/tmp/x",
        workspace_base="/tmp/ws",
        workstreams=[],
    )
    assert cfg.gates is None


def test_ready_to_needs_review_is_legal() -> None:
    # gates need a legal edge from READY to human review
    assert (
        WorkstreamStatus.NEEDS_REVIEW
        in WorkstreamStatus.valid_transitions()[WorkstreamStatus.READY]
    )


def test_pipeline_log_dir_honors_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path / "custom"))
    assert pipeline_log_dir() == tmp_path / "custom"


# ---------------------------------------------------------------- preflight


def _config(**gates: str) -> OrchestratorConfig:
    return OrchestratorConfig.model_validate(
        {
            "project": "demo",
            "repo_url": "https://example.com/x",
            "repo_path": "/tmp/x",
            "workspace_base": "/tmp/ws",
            "workstreams": [],
            "gates": dict(gates) or None,
        }
    )


def test_preflight_gates_missing_binary_is_error(tmp_path: Path) -> None:
    from maestro.preflight import validate_project

    report = validate_project(
        _config(steward_bin=str(tmp_path / "nope")), check_fs=True
    )
    assert any(i.code == "gates-steward-missing" for i in report.errors)


def test_preflight_gates_missing_risk_model_is_error(
    fake_steward: Path, tmp_path: Path
) -> None:
    from maestro.preflight import validate_project

    report = validate_project(
        _config(
            steward_bin=str(fake_steward),
            risk_model=str(tmp_path / "no-model.yaml"),
        ),
        check_fs=True,
    )
    assert any(i.code == "gates-risk-model-missing" for i in report.errors)


def test_preflight_gates_ok_with_valid_binary(fake_steward: Path) -> None:
    from maestro.preflight import validate_project

    report = validate_project(_config(steward_bin=str(fake_steward)), check_fs=True)
    assert not any(i.code.startswith("gates-") for i in report.errors)


def test_preflight_gates_skips_fs_checks_when_no_fs(tmp_path: Path) -> None:
    from maestro.preflight import validate_project

    report = validate_project(
        _config(steward_bin=str(tmp_path / "nope")), check_fs=False
    )
    assert not any(i.code.startswith("gates-") for i in report.errors)


# ---------------------------------------------------------------- orchestrator hooks


class _FakeKeeper:
    def __init__(self, allow: bool) -> None:
        from maestro.gates import GateDecision

        self._decision = GateDecision(
            allow=allow, reason=None if allow else "gates: blocked. marker"
        )
        self.calls: list[str] = []

    async def evaluate_ex_ante(self, *a: object, **kw: object):
        self.calls.append("ex_ante")
        return self._decision

    async def evaluate_ex_post(self, *a: object, **kw: object):
        self.calls.append("ex_post")
        return self._decision


def _workstream(status: WorkstreamStatus):
    from maestro.models import Workstream

    return Workstream(
        id="ws-1",
        title="t",
        description="d",
        scope=["src/**"],
        status=status,
        branch="feature/ws-1",
    )


async def test_orchestrator_ex_ante_block_routes_to_needs_review(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from maestro.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._gates = _FakeKeeper(allow=False)  # type: ignore[assignment]
    orch._db = MagicMock()
    orch._db.update_workstream_status = AsyncMock()
    orch._logger = MagicMock()
    allowed = await orch._gate_ex_ante("ws-1", _workstream(WorkstreamStatus.READY))
    assert allowed is False
    kwargs = orch._db.update_workstream_status.call_args.kwargs
    args = orch._db.update_workstream_status.call_args.args
    assert (
        WorkstreamStatus.NEEDS_REVIEW in args
        or kwargs.get("status") == WorkstreamStatus.NEEDS_REVIEW
    )
    assert "gates:" in kwargs.get("error_message", "")


async def test_orchestrator_gates_disabled_is_noop() -> None:
    from unittest.mock import MagicMock

    from maestro.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._gates = None
    orch._db = MagicMock()
    assert await orch._gate_ex_ante("ws-1", _workstream(WorkstreamStatus.READY))


async def test_structurally_invalid_steward_json_fails_closed(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # Copilot (PR #73): JSON that parses but lacks the contract shape must
    # become an error verdict, not a KeyError escaping the guard.
    fake_steward.write_text('#!/bin/sh\necho "{}"\n')  # noqa: ASYNC240
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not decision.allow
    assert any(r.verdict == "error" for r in decision.records)


async def test_non_object_steward_json_fails_closed(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    fake_steward.write_text('#!/bin/sh\necho "[1, 2]"\n')  # noqa: ASYNC240
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not decision.allow


# ------------------------------------------- gates v1.1 (governed-run findings H-3..H-5)


async def test_h3_approval_survives_via_verdict_store(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # H-3: the error_message marker gets overwritten between phases; the
    # verdict store is the durable approval memory. A prior recorded block
    # for the SAME (workstream, phase, sha) counts as operator approval on
    # re-evaluation even when prior_error carries no marker.
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not first.allow
    # prior_error=None — e.g. the ex-post phase overwrote the marker.
    second = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert second.allow, "recorded block + re-queue must count as approval"
    approvals = [r for r in second.records if r.gate_id == "human.owner_approval"]
    assert approvals and approvals[0].verdict == "pass"


async def test_h3_store_approval_still_invalidated_by_new_sha(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not first.allow
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (repo / "src" / "a.py").write_text("x = 3\n")
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(repo), "commit", "-am", "new"],
        check=True,
        capture_output=True,
        env=env,
    )
    second = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not second.allow, "a new commit must invalidate the stored approval"


async def test_h3_other_workstream_block_does_not_approve(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-other", ["src/**"], prior_error=None)
    assert not first.allow
    second = await keeper.evaluate_ex_ante("ws-1", ["src/**"], prior_error=None)
    assert not second.allow, "approval memory is per workstream"


async def test_h4_orchestrator_managed_paths_do_not_violate_scope(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # H-4: Maestro itself commits spec/** + spec-runner.config.yaml into the
    # branch; those infra paths must not trip the declared-scope check.
    workspace = _branch_with_change(repo, "src/a.py", "x = 9\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    spec = workspace / "spec"
    spec.mkdir(exist_ok=True)
    (spec / "tasks.md").write_text("# t\n")
    (workspace / "spec-runner.config.yaml").write_text("a: 1\n")
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(workspace), "add", "-A"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(workspace), "commit", "-m", "maestro: add spec"],
        check=True,
        capture_output=True,
        env=env,
    )
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, prior_error=None
    )
    assert decision.allow, (decision.reason, [r.note for r in decision.records])


def test_h5_workstream_approve_cli_flips_needs_review_to_ready(tmp_path: Path) -> None:
    import asyncio as _asyncio

    from maestro.database import Database
    from maestro.models import Workstream, WorkstreamStatus

    async def scenario() -> tuple[str, str | None]:
        db = Database(tmp_path / "m.db")
        await db.connect()
        await db.initialize_schema()
        ws = Workstream(
            id="ws-1",
            title="t",
            description="d",
            scope=["src/**"],
            status=WorkstreamStatus.PENDING,
            branch="feature/ws-1",
        )
        try:
            await db.create_workstream(ws)
            await db.update_workstream_status(
                "ws-1",
                WorkstreamStatus.NEEDS_REVIEW,
                error_message="gates: ... marker",
            )
            from maestro.cli import _approve_workstream

            await _approve_workstream(db, "ws-1")
            after = await db.get_workstream("ws-1")
            return after.status, after.error_message
        finally:
            await db.close()

    status, error_message = _asyncio.run(scenario())
    assert status == WorkstreamStatus.READY
    assert error_message and "marker" in error_message, "marker must be preserved"
