"""Gates-in-DAG runtime (steward WS-006 handoff, items M-1..M-3).

Guard hooks for two workstream transition edges: **ex-ante** before
READY -> RUNNING (classify the declared ``workstream.scope``) and **ex-post**
before RUNNING -> MERGING (classify the actual diff, catch scope violations).
Tiers come from ``steward risk-classify`` — a single source of truth; Maestro
never computes risk itself (DESIGN-610/612).

Semantics (DESIGN-606, fail-closed): a mandatory gate with a missing or
errored verdict blocks the transition — at every tier. A blocked workstream
routes to NEEDS_REVIEW with the approval marker in ``error_message``; a human
re-queueing it (NEEDS_REVIEW -> READY) *is* the owner approval for that exact
phase + SHA — a new commit changes the SHA and invalidates the approval
(DESIGN-608, M-3). Gates v1.2 (H-6/H-7): an approved ex-post block resumes at
the ex-post edge (see orchestrator._try_resume_ex_post); the infra-path
exclusion covers only maestro-prefixed artifacts, and the approval marker in
error_message clears only at DONE. Every evaluation appends a verdict-record to
``logs/<ULID>/gate_verdicts.jsonl`` (M-1); records are addressable via
EvidenceRef ``kind=gate-verdict``. Gates whose enforcement point lies outside
these two edges (branch protection, PR reviews) are recorded as advisory
annotations, not blocks (M-2) — their transition belongs to the git host, not
to Maestro's table. Gates v1.3 (H-9): the ``gate_approvals`` DB table is the
single authority for "was this (workstream, phase, sha) approved" — the
marker in ``error_message`` is operator UX and the H-6 resume-position
signal only, never an authorization source.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from maestro._vendor.obs import current_pipeline_id
from maestro.models import SPEC_PREFIX


if TYPE_CHECKING:
    from maestro.models import GatesConfig


__all__ = [
    "APPROVAL_MARKER_PREFIX",
    "BLOCK_REASON_PREFIX",
    "ApprovalMarker",
    "GateDecision",
    "GateKeeper",
    "GateVerdictRecord",
    "parse_approval_marker",
    "pipeline_log_dir",
    "preserve_approval_marker",
]

APPROVAL_MARKER_PREFIX = "gates:approval-required"
BLOCK_REASON_PREFIX = "gates: human.owner_approval required"

_MARKER_RE = re.compile(
    re.escape(APPROVAL_MARKER_PREFIX)
    + r" phase=(ex_ante|ex_post) sha=([0-9a-fA-F]{7,64})"
)


class ApprovalMarker(BaseModel):
    """Parsed `gates:approval-required phase=<p> sha=<sha>` marker (H-6)."""

    model_config = ConfigDict(frozen=True)

    phase: Literal["ex_ante", "ex_post"]
    sha: str


def parse_approval_marker(error_message: str | None) -> ApprovalMarker | None:
    """Extract the gates approval marker from a stored block reason.

    Returns None when the message is empty or carries no well-formed
    marker. The marker is the durable half of the approval memory: it
    lives in the workstream row and survives orchestrator restarts,
    unlike the verdict store bound to one run's logs/<ULID>/ directory.
    """
    if not error_message:
        return None
    match = _MARKER_RE.search(error_message)
    if match is None:
        return None
    phase = match.group(1)
    assert phase in ("ex_ante", "ex_post")  # regex guarantees; narrows type
    return ApprovalMarker(phase=phase, sha=match.group(2))


def preserve_approval_marker(new_message: str, prior: str | None) -> str:
    """Carry an approval marker from a prior error_message into a new one.

    H-6 position retention (NOT authority — that lives in gate_approvals):
    losing the marker to a failure/shutdown message costs a wasteful full
    respawn. Idempotent: extracts the first marker from `prior` and appends
    it once; a marker already present in `new_message` is never duplicated.
    """
    if not prior:
        return new_message
    match = _MARKER_RE.search(prior)
    if match is None:
        return new_message
    marker = match.group(0)
    if marker in new_message:
        return new_message
    return f"{new_message} | {marker}"


# Paths Maestro itself materializes in the worktree (spec generation +
# executor runtime state); they are infra, not the agent's change —
# excluded from ex-post classification and the declared-scope check
# (H-4, narrowed in gates v1.2/H-7). Deliberately NOT excluded:
# `spec-runner.config.yaml` — in a repo that tracks its own config,
# Maestro's overwrite must surface as a scope violation (fail-closed
# backstop); in a repo without one the file is untracked+ignored and
# never enters the diff. `spec/.{prefix}` covers the dot-before-prefix
# harness files (task-history, spec lock) spec-runner writes (H-8).
_ORCHESTRATOR_MANAGED = (
    f"spec/{SPEC_PREFIX}",
    f"spec/.{SPEC_PREFIX}",
    "spec/.executor-",
)


def _orchestrator_managed(path: str) -> bool:
    return path.startswith(_ORCHESTRATOR_MANAGED)


_STEWARD_ENV = "MAESTRO_STEWARD_BIN"

# Gates steward may demand that are enforced beyond Maestro's two edges:
# recorded as advisory annotations here, enforced by the git host / CI there.
_DEFERRED_GATES = {
    "git.required_reviews": "enforced at the PR stage (branch protection)",
    "human.transition_approval": "enforced at the PR stage (human merge)",
    "steward.gate_check": "runs in the target repo's CI, not at this edge",
}


class GateVerdictRecord(BaseModel):
    """One appended line of logs/<ULID>/gate_verdicts.jsonl (DESIGN-607)."""

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    obligation: Literal["mandatory", "advisory"]
    verdict: Literal["pass", "fail", "waived", "error", "missing"]
    tier: str | None = None
    phase: Literal["ex_ante", "ex_post"]
    sha: str | None = None
    risk_model_version: str | None = None
    ts: str
    workstream_id: str
    note: str | None = None


class GateDecision(BaseModel):
    """Outcome of one guard evaluation."""

    allow: bool
    reason: str | None = None
    records: list[GateVerdictRecord] = Field(default_factory=list)


def pipeline_log_dir() -> Path:
    """The pipeline's logs/<ULID>/ directory (mirrors vendored obs logic)."""
    env_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if env_dir:
        return Path(env_dir)
    pipeline = (
        current_pipeline_id()
        or os.environ.get("ORCHESTRA_PIPELINE_ID")
        or "no-pipeline"
    )
    return Path.cwd() / "logs" / pipeline


def _validated_classification(decoded: object) -> dict | str:
    """Shape-check the risk-classify output (DESIGN-610) — fail-closed.

    JSON that parses but lacks the contract shape must become an error
    verdict, not a KeyError escaping the guard.
    """
    if not isinstance(decoded, dict):
        return f"risk-classify output is not an object: {type(decoded).__name__}"
    tier = decoded.get("tier")
    if not isinstance(tier, str) or not tier:
        return "risk-classify output missing 'tier'"
    gates = decoded.get("mandatory_gates", [])
    flags = decoded.get("flags", [])
    if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
        return "risk-classify output: 'mandatory_gates' must be a list of strings"
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        return "risk-classify output: 'flags' must be a list of strings"
    return decoded


class GateKeeper:
    """Evaluates gates at transition edges and persists verdict-records."""

    def __init__(
        self,
        config: GatesConfig,
        *,
        project: str,
        repo_path: Path,
        base_branch: str,
        log_dir: Path,
    ) -> None:
        self._config = config
        self._project = project
        self._repo_path = repo_path
        self._base_branch = base_branch
        self._log_dir = log_dir

    # ------------------------------------------------------------ public

    async def evaluate_ex_ante(
        self,
        workstream_id: str,
        scope: list[str],
        approvals: set[tuple[str, str]],
    ) -> GateDecision:
        """Guard for READY -> RUNNING: classify the declared scope (REQ-605)."""
        sha = await self._git_sha(self._repo_path)
        payload = {"project": self._project, "sha": sha, "scope": scope}
        classification = await self._classify("--declared", payload)
        return self._decide("ex_ante", workstream_id, sha, classification, approvals)

    async def evaluate_ex_post(
        self,
        workstream_id: str,
        scope: list[str],
        workspace: Path,
        approvals: set[tuple[str, str]],
    ) -> GateDecision:
        """Guard for RUNNING -> MERGING: classify the actual diff vs base."""
        sha = await self._git_sha(workspace)
        paths = [
            p
            for p in await self._git_diff_paths(workspace)
            if not _orchestrator_managed(p)
        ]
        payload = {
            "project": self._project,
            "sha": sha,
            "paths": paths,
            "declared_scope": scope,
        }
        classification = await self._classify("--no-fs", payload)
        return self._decide("ex_post", workstream_id, sha, classification, approvals)

    # ------------------------------------------------------------ decision

    def _decide(
        self,
        phase: Literal["ex_ante", "ex_post"],
        workstream_id: str,
        sha: str,
        classification: dict | str,
        approvals: set[tuple[str, str]],
    ) -> GateDecision:
        ts = datetime.now(UTC).isoformat()

        def record(
            gate_id: str,
            obligation: Literal["mandatory", "advisory"],
            verdict: Literal["pass", "fail", "waived", "error", "missing"],
            tier: str | None = None,
            risk_model_version: str | None = None,
            note: str | None = None,
        ) -> GateVerdictRecord:
            return GateVerdictRecord(
                gate_id=gate_id,
                obligation=obligation,
                verdict=verdict,
                tier=tier,
                risk_model_version=risk_model_version,
                note=note,
                phase=phase,
                sha=sha,
                ts=ts,
                workstream_id=workstream_id,
            )

        classify_gate = f"steward.risk_classify_{phase}"
        if isinstance(classification, str):  # error text — fail-closed
            records = [
                record(
                    gate_id=classify_gate,
                    obligation="mandatory",
                    verdict="error",
                    note=classification,
                )
            ]
            self._write(records)
            return GateDecision(
                allow=False,
                reason=f"gates: {classify_gate} error: {classification}",
                records=records,
            )

        tier = classification["tier"]
        model_version = classification.get("risk_model_version")
        flags = classification.get("flags", [])
        records = [
            record(
                gate_id=classify_gate,
                obligation="mandatory",
                verdict="pass",
                tier=tier,
                risk_model_version=model_version,
                note=(", ".join(flags) or None),
            )
        ]

        # Advisory annotations for gates enforced beyond these edges (M-2).
        for gate_id in classification.get("mandatory_gates", []):
            if gate_id in _DEFERRED_GATES:
                records.append(
                    record(
                        gate_id=gate_id,
                        obligation="advisory",
                        verdict="missing",
                        tier=tier,
                        risk_model_version=model_version,
                        note=_DEFERRED_GATES[gate_id],
                    )
                )
            elif gate_id == "maestro.validate_strict":
                records.append(
                    record(
                        gate_id=gate_id,
                        obligation="mandatory",
                        verdict="pass",
                        tier=tier,
                        risk_model_version=model_version,
                        note="preflight ran at orchestrator startup",
                    )
                )

        blocked_reason: str | None = None
        needs_approval = (
            tier in self._config.approval_tiers or "scope_violation" in flags
        )
        if needs_approval:
            marker = f"{APPROVAL_MARKER_PREFIX} phase={phase} sha={sha}"
            # gates v1.3 (H-9): the DB approvals set is the single authority;
            # the marker in the block message is operator UX + the H-6
            # position signal, never authorization.
            approved = (phase, sha) in approvals
            if approved:
                records.append(
                    record(
                        gate_id="human.owner_approval",
                        obligation="mandatory",
                        verdict="pass",
                        tier=tier,
                        risk_model_version=model_version,
                        note="operator re-queued after NEEDS_REVIEW (same sha)",
                    )
                )
            else:
                detail = (
                    f"scope violation ({', '.join(flags)})"
                    if "scope_violation" in flags
                    else f"tier={tier}"
                )
                records.append(
                    record(
                        gate_id="human.owner_approval",
                        obligation="mandatory",
                        verdict="missing",
                        tier=tier,
                        risk_model_version=model_version,
                        note=detail,
                    )
                )
                blocked_reason = (
                    f"{BLOCK_REASON_PREFIX} ({detail}); re-queue to approve. {marker}"
                )

        self._write(records)
        if blocked_reason is not None:
            return GateDecision(allow=False, reason=blocked_reason, records=records)
        return GateDecision(allow=True, records=records)

    # ------------------------------------------------------------ steward

    def _resolve_bin(self) -> str | None:
        candidate = self._config.steward_bin or os.environ.get(_STEWARD_ENV)
        if not candidate:
            return None
        path = Path(candidate).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
        return str(path)

    async def _classify(self, input_flag: str, payload: dict) -> dict | str:
        """Run `steward risk-classify`; return the parsed JSON or an error string."""
        binary = self._resolve_bin()
        if binary is None:
            return f"steward binary not found (gates.steward_bin or ${_STEWARD_ENV})"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle)
            input_path = handle.name
        cmd = [
            binary,
            "risk-classify",
            input_flag,
            input_path,
            "--profile",
            self._config.profile,
        ]
        if self._config.risk_model:
            cmd += ["--risk-model", self._config.risk_model]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                return (
                    f"risk-classify exited {process.returncode}: "
                    f"{stderr.decode(errors='replace').strip()[:500]}"
                )
            decoded = json.loads(stdout.decode())
            return _validated_classification(decoded)
        except (OSError, json.JSONDecodeError) as exc:
            return f"risk-classify failed: {exc}"
        finally:
            await asyncio.to_thread(Path(input_path).unlink, True)

    # ------------------------------------------------------------ git + store

    async def _git_sha(self, repo: Path) -> str:
        out = await self._git(repo, "rev-parse", "HEAD")
        return out.strip()

    async def _git_diff_paths(self, repo: Path) -> list[str]:
        out = await self._git(repo, "diff", "--name-only", f"{self._base_branch}..HEAD")
        return [line for line in out.splitlines() if line]

    @staticmethod
    async def _git(repo: Path, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            msg = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
        return stdout.decode()

    def _write(self, records: list[GateVerdictRecord]) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / "gate_verdicts.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for rec in records:
                handle.write(json.dumps(rec.model_dump(exclude_none=True)) + "\n")
