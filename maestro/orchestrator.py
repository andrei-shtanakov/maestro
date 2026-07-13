"""Multi-process orchestrator for Maestro.

This module provides the Orchestrator class that coordinates
multiple spec-runner processes, each running in its own git
worktree. It handles the full lifecycle: decomposition, workspace
setup, process spawning, monitoring, and PR creation.
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from maestro._vendor.obs import child_env, current_pipeline_id, span
from maestro.database import Database
from maestro.decomposer import ProjectDecomposer
from maestro.gates import (
    BLOCK_REASON_PREFIX,
    ApprovalMarker,
    GateDecision,
    GateKeeper,
    parse_approval_marker,
    pipeline_log_dir,
    preserve_approval_marker,
)
from maestro.git import GitError, MergeConflictError
from maestro.merge_logs import merge_logs_dir
from maestro.models import (
    SPEC_PREFIX,
    OrchestratorConfig,
    Workstream,
    WorkstreamConfig,
    WorkstreamStatus,
)
from maestro.pr_manager import PRManager, PRManagerError
from maestro.spec_runner import read_executor_state
from maestro.workspace import WorkspaceManager, ensure_harness_excludes


class OrchestratorError(Exception):
    """Base exception for orchestrator errors."""


_SPAWNING_SENTINEL = -1
"""Placeholder pid written into ``process_pid`` / ``generation_pid`` BEFORE a
subprocess spawn and overwritten with the real pid after. A recovery that finds
it treats the workstream as a possible live orphan (a spawn was in progress at
the crash). Never passed to ``os.kill`` — see ``_maybe_live_orphan`` and the
``pid <= 0`` guard in ``_is_pid_alive``."""


def _is_pid_alive(pid: int) -> bool:
    """True if a process with this pid exists (signal 0 probes without killing).

    ProcessLookupError means it is gone; PermissionError means it exists but
    we may not signal it (still alive).
    """
    if pid <= 0:
        # Never signal a non-positive pid: os.kill(0/-1, …) would hit the
        # caller's process group / every process. A real pid is always > 0.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _maybe_live_orphan(pid: int | None) -> bool:
    """True if the recorded pid indicates a possibly-live orphan: the spawning
    sentinel (a spawn was in progress at the crash) or a still-alive real pid.

    Checks the sentinel FIRST so it is never passed to os.kill.
    """
    if pid == _SPAWNING_SENTINEL:
        return True
    return pid is not None and _is_pid_alive(pid)


_STRANDED_INFLIGHT = (
    WorkstreamStatus.DECOMPOSING,
    WorkstreamStatus.RUNNING,
    WorkstreamStatus.MERGING,
    WorkstreamStatus.PR_CREATED,
)


@dataclass
class RunningWorkstream:
    """Represents a currently running workstream process."""

    workstream: Workstream
    process: asyncio.subprocess.Process
    started_at: datetime
    workspace_path: Path
    log_file: Path


@dataclass
class OrchestratorStats:
    """Statistics for an orchestration run."""

    total_workstreams: int = 0
    completed: int = 0
    failed: int = 0
    prs_created: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(UTC))


class Orchestrator:
    """Coordinates multiple spec-runner processes.

    Main loop:
    1. Decompose project into workstreams (if needed)
    2. Resolve ready workstreams from DAG
    3. Create workspace + spawn spec-runner for each
    4. Monitor processes, read progress
    5. On completion: push + create PR + cleanup
    """

    def __init__(
        self,
        db: Database,
        workspace_mgr: WorkspaceManager,
        decomposer: ProjectDecomposer,
        pr_manager: PRManager,
        config: OrchestratorConfig,
        log_dir: Path | None = None,
    ) -> None:
        """Initialize orchestrator.

        Args:
            db: Database for state persistence.
            workspace_mgr: Manager for worktree workspaces.
            decomposer: Project decomposer for spec gen.
            pr_manager: PR creation manager.
            config: Orchestrator configuration.
            log_dir: Directory for log files.
        """
        self._db = db
        self._workspace_mgr = workspace_mgr
        self._decomposer = decomposer
        self._pr_manager = pr_manager
        self._config = config
        self._log_dir = log_dir or Path(config.repo_path).expanduser() / "logs"
        self._gates: GateKeeper | None = None
        if config.gates is not None:
            self._gates = GateKeeper(
                config.gates,
                project=config.project,
                repo_path=Path(config.repo_path).expanduser(),
                base_branch=config.base_branch,
                log_dir=pipeline_log_dir(),
            )

        self._running: dict[str, RunningWorkstream] = {}
        self._generating: dict[str, asyncio.Task[None]] = {}
        self._shutdown_grace_seconds: float = 5.0
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logger = logging.getLogger(__name__)
        self._stats = OrchestratorStats()

    @property
    def is_running(self) -> bool:
        """Check if orchestrator is running."""
        return self._loop is not None and not self._shutdown_requested

    async def run(self) -> OrchestratorStats:
        """Run the orchestrator main loop.

        Returns:
            Statistics for the orchestration run.

        Raises:
            OrchestratorError: If database not connected.
        """
        if not self._db.is_connected:
            msg = "Database must be connected"
            raise OrchestratorError(msg)

        self._loop = asyncio.get_running_loop()
        self._setup_signal_handlers()
        self._log_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Step 1: Ensure workstreams exist
            await self._ensure_workstreams()

            # Step 1b: Reconcile workstreams stranded by a prior hard crash
            # (resume path) so the main loop can advance them.
            await self._recover_stranded_workstreams()

            # Step 2: Main loop
            await self._main_loop()
        finally:
            await self._cleanup()
            _pipeline_id = current_pipeline_id()
            if _pipeline_id:
                _log_dir = Path(
                    os.environ.get("ORCHESTRA_LOG_DIR") or f"logs/{_pipeline_id}"
                )
                if _log_dir.exists():  # noqa: ASYNC240
                    with contextlib.suppress(Exception):
                        merge_logs_dir(_log_dir)

        return self._stats

    async def _recover_stranded_workstreams(self) -> int:
        """Reconcile workstreams stranded by a hard crash so the resume loop
        can advance them. In-flight strands reset to READY (no retry, no
        error_message); a RUNNING workstream whose recorded process is still
        alive goes to NEEDS_REVIEW instead (never re-run over a live orphan);
        FAILED workstreams reconcile by the retry rule. Best-effort per
        workstream; never raises."""
        recovered = 0

        for state in _STRANDED_INFLIGHT:
            for w in await self._db.get_workstreams_by_status(state):
                try:
                    orphan_pid = (
                        w.process_pid
                        if state is WorkstreamStatus.RUNNING
                        else w.generation_pid
                        if state is WorkstreamStatus.DECOMPOSING
                        else None
                    )
                    live_orphan = _maybe_live_orphan(orphan_pid)
                    if live_orphan:
                        if orphan_pid == _SPAWNING_SENTINEL:
                            self._logger.warning(
                                "Workstream '%s' stranded in %s with a spawn in "
                                "progress at the crash — state uncertain (a "
                                "subprocess may or may not be running); sending "
                                "to NEEDS_REVIEW, verify before resuming",
                                w.id,
                                state.value,
                            )
                        else:
                            self._logger.warning(
                                "Workstream '%s' stranded in %s with a live "
                                "process (pid %s) after restart — sending to "
                                "NEEDS_REVIEW; verify and clean it up before resume",
                                w.id,
                                state.value,
                                orphan_pid,
                            )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.FAILED
                        )
                        await self._db.update_workstream_status(
                            w.id,
                            WorkstreamStatus.NEEDS_REVIEW,
                            expected_status=WorkstreamStatus.FAILED,
                            process_pid=None,
                            generation_pid=None,
                        )
                        # Parked for review — signal via exit code + summary,
                        # matching _handle_failure's NEEDS_REVIEW accounting.
                        self._stats.failed += 1
                    elif state is WorkstreamStatus.DECOMPOSING:
                        self._logger.info(
                            "Recovering workstream '%s' from stranded "
                            "DECOMPOSING -> READY",
                            w.id,
                        )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.READY
                        )
                    else:
                        # RUNNING (dead) / MERGING / PR_CREATED: cannot go
                        # directly to READY, reset via FAILED.
                        self._logger.info(
                            "Recovering workstream '%s' from stranded %s -> READY",
                            w.id,
                            state.value,
                        )
                        await self._db.update_workstream_status(
                            w.id, WorkstreamStatus.FAILED
                        )
                        await self._db.update_workstream_status(
                            w.id,
                            WorkstreamStatus.READY,
                            expected_status=WorkstreamStatus.FAILED,
                        )
                    recovered += 1
                except Exception as e:
                    self._logger.error("Failed to recover workstream '%s': %s", w.id, e)

        # FAILED reconciliation (genuine failures resting mid-_handle_failure).
        # Runs after the in-flight loop, so in-flight resets that pass through
        # FAILED have already reached their final state.
        for w in await self._db.get_workstreams_by_status(WorkstreamStatus.FAILED):
            try:
                if _maybe_live_orphan(w.process_pid) or _maybe_live_orphan(
                    w.generation_pid
                ):
                    # A FAILED row can be an in-flight reset interrupted mid
                    # two-write (X->FAILED committed, target write lost). If its
                    # recorded process_pid (RUNNING orphan) or generation_pid
                    # (DECOMPOSING orphan) is alive OR the spawning sentinel, it
                    # may be a live orphan — never reset to READY. Park for
                    # review. The NEEDS_REVIEW write below clears both pids.
                    target = WorkstreamStatus.NEEDS_REVIEW
                elif w.error_message is not None and w.error_message.startswith(
                    BLOCK_REASON_PREFIX
                ):
                    # `_gate_ex_post` blocks with TWO writes: RUNNING ->
                    # FAILED-with-block-reason, then FAILED -> NEEDS_REVIEW.
                    # A crash between them strands a FAILED row whose
                    # error_message IS the gate-block reason. Finish the
                    # interrupted write as NEEDS_REVIEW. NOTE (v1.3): the
                    # predicate is the BLOCK_REASON_PREFIX, not marker
                    # presence — `_handle_failure` now APPENDS markers to
                    # ordinary failure messages (H-6 position retention),
                    # and those must follow the normal retry rule.
                    target = WorkstreamStatus.NEEDS_REVIEW
                else:
                    target = (
                        WorkstreamStatus.READY
                        if w.can_retry()
                        else WorkstreamStatus.NEEDS_REVIEW
                    )
                self._logger.info(
                    "Reconciling FAILED workstream '%s' -> %s",
                    w.id,
                    target.value,
                )
                if target is WorkstreamStatus.NEEDS_REVIEW:
                    await self._db.update_workstream_status(
                        w.id,
                        WorkstreamStatus.NEEDS_REVIEW,
                        expected_status=WorkstreamStatus.FAILED,
                        process_pid=None,
                        generation_pid=None,
                    )
                    # Parked for review — signal via exit code + summary.
                    self._stats.failed += 1
                else:
                    await self._db.update_workstream_status(
                        w.id,
                        WorkstreamStatus.READY,
                        expected_status=WorkstreamStatus.FAILED,
                    )
                recovered += 1
            except Exception as e:
                self._logger.error(
                    "Failed to reconcile FAILED workstream '%s': %s", w.id, e
                )

        if recovered:
            self._logger.info(
                "Recovered %d stranded workstream(s) on startup", recovered
            )
        return recovered

    async def _ensure_workstreams(self) -> None:
        """Ensure workstreams are in the database.

        If no workstreams exist, run decomposition.
        """
        existing = await self._db.get_all_workstreams()

        if existing:
            self._logger.info("Found %d existing workstreams", len(existing))
            self._stats.total_workstreams = len(existing)
            return

        # Use manually specified workstreams from config
        if self._config.workstreams:
            self._logger.info(
                "Creating %d workstreams from config",
                len(self._config.workstreams),
            )
            await self._create_workstreams_from_configs(self._config.workstreams)
            return

        # Auto-decompose
        if not self._config.description:
            msg = "No workstreams in config and no project description for auto-decomposition"
            raise OrchestratorError(msg)

        self._logger.info("Auto-decomposing project")
        configs = self._decomposer.decompose(self._config.description)
        await self._create_workstreams_from_configs(configs)

    async def _create_workstreams_from_configs(
        self, configs: list[WorkstreamConfig]
    ) -> None:
        """Create Workstream records in DB from configs."""
        for config in configs:
            workstream = Workstream.from_config(
                config,
                branch_prefix=self._config.branch_prefix,
            )
            await self._db.create_workstream(workstream)

        self._stats.total_workstreams = len(configs)
        self._logger.info("Created %d workstreams in database", len(configs))

    async def _main_loop(self) -> None:
        """Main orchestration loop."""
        poll_interval = 2.0

        while not self._shutdown_requested:
            # Get completed workstream IDs
            completed_ids = await self._get_completed_ids()

            # Check if all done
            if await self._all_workstreams_complete():
                self._logger.info("All workstreams complete")
                break

            # Resolve ready workstreams
            ready_ids = await self._resolve_ready(completed_ids)

            # Spawn up to max_concurrent
            await self._spawn_ready(ready_ids)

            # Monitor running processes
            await self._monitor_running()

            # Wait before next iteration
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=poll_interval,
                )

    async def _get_completed_ids(self) -> set[str]:
        """Get IDs of completed workstreams."""
        done = await self._db.get_workstreams_by_status(WorkstreamStatus.DONE)
        return {z.id for z in done}

    async def _all_workstreams_complete(self) -> bool:
        """Check if all workstreams are in terminal states."""
        all_z = await self._db.get_all_workstreams()
        terminal = {
            WorkstreamStatus.DONE,
            WorkstreamStatus.ABANDONED,
        }

        for z in all_z:
            if z.status not in terminal:
                if z.status == WorkstreamStatus.NEEDS_REVIEW:
                    continue
                return False

        return True

    async def _resolve_ready(self, completed_ids: set[str]) -> list[str]:
        """Resolve workstreams that are ready to run.

        A workstream is ready when:
        - Status is PENDING or READY
        - All dependencies are completed
        - Not already running
        """
        all_z = await self._db.get_all_workstreams()
        ready: list[str] = []

        for z in all_z:
            if z.id in self._running:
                continue
            if z.id in self._generating:
                continue
            if z.status not in (
                WorkstreamStatus.PENDING,
                WorkstreamStatus.READY,
            ):
                continue

            # Check all dependencies completed
            if z.depends_on and not set(z.depends_on).issubset(completed_ids):
                continue

            ready.append(z.id)

        # Sort by priority (descending)
        all_by_id = {z.id: z for z in all_z}
        ready.sort(
            key=lambda zid: all_by_id[zid].priority,
            reverse=True,
        )

        return ready

    async def _spawn_ready(self, ready_ids: list[str]) -> None:
        """Launch background spec generation for ready workstreams up to the
        concurrency limit. Generation runs off the main loop so monitoring
        and shutdown stay responsive."""
        available = max(
            0,
            self._config.max_concurrent - len(self._running) - len(self._generating),
        )
        for zid in ready_ids[:available]:
            if self._shutdown_requested:
                break
            if zid in self._generating or zid in self._running:
                continue
            self._generating[zid] = asyncio.create_task(self._generate_and_launch(zid))

    async def _generate_and_launch(self, workstream_id: str) -> None:
        """Background task: generate the spec, then spawn `run --all`.

        - Cancellation (shutdown) → return the workstream to READY, no retry
          consumed, and propagate the cancel.
        - Any other error → _handle_failure (retry accounting).
        - The _generating slot is always freed in `finally`.
        """
        try:
            await self._spawn_workstream(workstream_id)
        except asyncio.CancelledError:
            # Clear generation_pid atomically in the same READY write: on the
            # cancel path the `finally` clear can itself be interrupted by a
            # re-raised CancelledError before its awaits complete, so cleanup
            # must not depend on it here.
            with contextlib.suppress(Exception):
                await self._db.update_workstream_status(
                    workstream_id,
                    WorkstreamStatus.READY,
                    generation_pid=None,
                )
            raise
        except Exception as e:
            self._logger.error(
                "Failed to generate spec or launch workstream '%s': %s",
                workstream_id,
                e,
            )
            await self._handle_failure(workstream_id, str(e))
        finally:
            self._generating.pop(workstream_id, None)
            # Clear the generation pid on every exit (success/cancel/failure);
            # a stale pid only pollutes REST/dashboard, but keep it clean.
            # Same-state write WITHOUT expected_status (update_workstream_status
            # does not validate transitions — an expected_status here would
            # wrongly block the reset after READY/FAILED).
            with contextlib.suppress(Exception):
                w = await self._db.get_workstream(workstream_id)
                if w.generation_pid is not None:
                    await self._db.update_workstream_status(
                        workstream_id, w.status, generation_pid=None
                    )

    async def _spawn_workstream(self, workstream_id: str) -> None:
        """Spawn a spec-runner process for a workstream."""
        workstream = await self._db.get_workstream(workstream_id)

        # Gates v1.2 (H-6): an operator-approved ex-post block resumes at
        # the ex-post edge over the untouched worktree. Replaying the
        # pipeline would regenerate the spec, mint a new sha, and void the
        # approval (DESIGN-608). Must run before DECOMPOSING and before the
        # spawning sentinel — a resume never spawns anything.
        marker = parse_approval_marker(workstream.error_message)
        if (
            marker is not None
            and marker.phase == "ex_post"
            and await self._try_resume_ex_post(workstream, marker)
        ):
            return

        # Transition to DECOMPOSING; write the spawning sentinel up front — it
        # marks a spawn-in-progress AND overwrites any stale prior generation
        # pid (re-decompose).
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.DECOMPOSING,
            expected_status=workstream.status,
            generation_pid=_SPAWNING_SENTINEL,
        )

        # Create workspace
        if not self._workspace_mgr.workspace_exists(workstream_id):
            workspace = self._workspace_mgr.create_workspace(
                workstream_id, workstream.branch
            )
        else:
            workspace = self._workspace_mgr.get_workspace_path(workstream_id)

        # Update workspace path in DB
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.DECOMPOSING,
            workspace_path=str(workspace),
        )

        # H-7: keep every harness artifact untracked in the target repo —
        # repo-local ignore block, shared by all linked worktrees.
        await asyncio.get_running_loop().run_in_executor(
            None, ensure_harness_excludes, workspace
        )

        # Setup spec-runner config BEFORE generation so `plan --full`
        # writes prefix-namespaced spec files from the start (H-7).
        executor_config = self._config.spec_runner.to_executor_config()
        # Set main_branch to the workstream branch (so spec-runner
        # merges subtask branches back to it)
        executor_config.setdefault("executor", {})["main_branch"] = workstream.branch
        self._workspace_mgr.setup_spec_runner(workspace, executor_config)

        # Generate spec for this workstream
        # Always regenerate: the repo may already have spec/maestro-tasks.md
        # from a previous run or different project phase
        workstream_config = WorkstreamConfig(
            id=workstream.id,
            title=workstream.title,
            description=workstream.description,
            scope=workstream.scope,
            depends_on=workstream.depends_on,
            priority=workstream.priority,
        )

        async def _on_gen_pid(pid: int) -> None:
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.DECOMPOSING,
                generation_pid=pid,
            )

        await self._decomposer.generate_spec(
            workstream_config, workspace, on_pid=_on_gen_pid
        )

        # Transition to READY then RUNNING
        await self._db.update_workstream_status(workstream_id, WorkstreamStatus.READY)

        # Gates (WS-006): ex-ante guard over the declared scope. A block
        # routes to NEEDS_REVIEW; the operator re-queueing it approves the
        # gate for this exact repo SHA (see maestro/gates.py).
        if not await self._gate_ex_ante(workstream_id, workstream):
            return

        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.RUNNING,
            expected_status=WorkstreamStatus.READY,
            process_pid=_SPAWNING_SENTINEL,
        )

        # Spawn spec-runner
        log_file = self._log_dir / f"{workstream_id}.log"

        cmd = ["spec-runner", "run", "--all", "--spec-prefix", SPEC_PREFIX]

        # Add callback URL if REST API is running
        # (optional — we also poll state files)
        if self._config.callback_url:
            cmd.extend(["--callback-url", self._config.callback_url])

        log_fd = os.open(str(log_file), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        try:
            with span("task.execute", task_id=workstream_id):
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=workspace,
                    env={**os.environ, **child_env()},
                    stdout=log_fd,
                    stderr=asyncio.subprocess.STDOUT,
                )
        except BaseException:
            os.close(log_fd)
            raise

        # Register in _running BEFORE any further await, so a shutdown
        # cancellation can never orphan the spawned process: once it's
        # here, _cleanup's termination loop will reach it regardless of
        # where a later cancel lands.
        self._running[workstream_id] = RunningWorkstream(
            workstream=workstream.model_copy(
                update={
                    "status": WorkstreamStatus.RUNNING,
                    "workspace_path": str(workspace),
                }
            ),
            process=process,
            started_at=datetime.now(UTC),
            workspace_path=workspace,
            log_file=log_file,
        )

        # Update PID in DB
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.RUNNING,
            process_pid=process.pid,
        )

        self._logger.info(
            "Spawned spec-runner for '%s' (PID %d) in %s",
            workstream_id,
            process.pid,
            workspace,
        )

    def _merge_into_base(self, feature_branch: str) -> None:
        """Merge feature branch into base branch in the main repo.

        Prevents accumulation of unmerged branches that diverge and cause
        conflicts. Each workstream is merged immediately after completion so
        the next workstream sees all prior work.

        Verifies the main repo is on ``base_branch`` before merging (the
        Mode-2 worktree topology keeps it there); a wrong or detached branch
        raises rather than silently merging into the wrong place. On a merge
        failure the partial merge is aborted and the error raised so the
        caller can route the workstream to review instead of DONE.

        Raises:
            GitError: If the repo is not on ``base_branch``, or the merge
                fails for a non-conflict reason.
            MergeConflictError: If the merge has conflicts.
        """
        repo = Path(self._config.repo_path).expanduser()
        base = self._config.base_branch
        merge_env = {**os.environ, **child_env()}

        with span("task.execute", task_id=feature_branch):
            head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                env=merge_env,
                capture_output=True,
                text=True,
                check=False,
            )
            current_branch = head.stdout.strip()
            if head.returncode != 0 or current_branch != base:
                msg = (
                    f"Refusing to merge '{feature_branch}': main repo is on "
                    f"'{current_branch or '(unknown)'}', not base '{base}'. "
                    "The main repo must be checked out on the base branch."
                )
                raise GitError(msg)

            result = subprocess.run(
                ["git", "merge", feature_branch, "--no-edit"],
                cwd=repo,
                env=merge_env,
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode == 0:
            self._logger.info("Merged '%s' into '%s'", feature_branch, base)
            return

        # Abort the partial/conflicted merge so the base repo is left clean,
        # then raise so the caller routes the workstream to review, not DONE.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=repo,
            env=merge_env,
            capture_output=True,
            text=True,
            check=False,
        )
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        # git writes "CONFLICT (...)" to stdout, not stderr; combine both so
        # the conflict marker is detected regardless of which stream git
        # chooses, and so the log and the raised error carry the same detail.
        detail = "\n".join(part for part in (stderr, stdout) if part)
        self._logger.warning(
            "Failed to merge '%s' into '%s': %s", feature_branch, base, detail
        )
        if "conflict" in detail.lower():
            msg = f"Merge conflicts merging '{feature_branch}' into '{base}':\n{detail}"
            raise MergeConflictError(msg)
        msg = f"Failed to merge '{feature_branch}' into '{base}':\n{detail}"
        raise GitError(msg)

    async def _monitor_running(self) -> None:
        """Monitor running spec-runner processes."""
        completed: list[str] = []

        for zid, running in self._running.items():
            # Read progress from state file
            await self._update_progress(zid, running)

            # Check if process finished (returncode is None while running)
            return_code = running.process.returncode

            if return_code is not None:
                await self._handle_completion(zid, running, return_code)
                completed.append(zid)

        for zid in completed:
            del self._running[zid]

    async def _update_progress(
        self,
        workstream_id: str,
        running: RunningWorkstream,
    ) -> None:
        """Read spec-runner state file for progress.

        Delegates to `maestro.spec_runner.read_executor_state()` so SQLite
        (spec-runner 2.0) and JSON (legacy) are handled uniformly. Runs the
        blocking read in a thread so the orchestrator loop stays responsive.
        """
        spec_dir = running.workspace_path / "spec"
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(
            None, read_executor_state, spec_dir, SPEC_PREFIX
        )

        if state is None:
            return

        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.RUNNING,
            subtask_progress=state.progress_label(),
        )

    async def _gate_ex_ante(self, workstream_id: str, workstream: Workstream) -> bool:
        """Evaluate the ex-ante gate; on block route READY -> NEEDS_REVIEW."""
        if self._gates is None:
            return True
        approvals = await self._db.list_gate_approvals(workstream_id)
        decision = await self._gates.evaluate_ex_ante(
            workstream_id, workstream.scope, approvals=approvals
        )
        if decision.allow:
            return True
        await self._route_gate_block(
            workstream_id, decision, expected=WorkstreamStatus.READY
        )
        return False

    @staticmethod
    async def _workspace_head(workspace: Path) -> str | None:
        """HEAD sha of a worktree, or None when unreadable."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return stdout.decode().strip()

    async def _try_resume_ex_post(
        self, workstream: Workstream, marker: ApprovalMarker
    ) -> bool:
        """Resume an ex-post-approved workstream at the ex-post edge (H-6).

        True only when the worktree still exists AND sits exactly at the
        approved sha; then the workstream goes READY -> RUNNING (explicitly
        clearing both pids so recovery never mistakes it for a live orphan)
        and re-enters the success continuation — ex-post re-evaluates,
        passes via the approval memory, and the normal MERGING -> PR ->
        merge -> DONE tail runs. False = caller proceeds with the full
        respawn (the approval is genuinely void, DESIGN-608).
        """
        if not self._workspace_mgr.workspace_exists(workstream.id):
            self._logger.warning(
                "Workstream '%s' has an ex-post approval for sha %s but no "
                "workspace; falling back to a full respawn",
                workstream.id,
                marker.sha[:12],
            )
            return False
        workspace = self._workspace_mgr.get_workspace_path(workstream.id)
        head = await self._workspace_head(workspace)
        if head != marker.sha:
            self._logger.warning(
                "Workstream '%s' worktree HEAD %s != approved sha %s; the "
                "approval is void (DESIGN-608), falling back to a full respawn",
                workstream.id,
                (head or "unreadable")[:12],
                marker.sha[:12],
            )
            return False
        await self._db.update_workstream_status(
            workstream.id,
            WorkstreamStatus.RUNNING,
            expected_status=workstream.status,
            process_pid=None,
            generation_pid=None,
            workspace_path=str(workspace),
        )
        self._logger.info(
            "Resuming workstream '%s' at the ex-post gate (approved sha %s)",
            workstream.id,
            marker.sha[:12],
        )
        await self._handle_success(workstream.id, workspace)
        return True

    async def _gate_ex_post(
        self,
        workstream_id: str,
        workstream: Workstream,
        workspace_path: Path,
    ) -> bool:
        """Evaluate the ex-post gate; on block route RUNNING -> FAILED -> NEEDS_REVIEW."""
        if self._gates is None:
            return True
        approvals = await self._db.list_gate_approvals(workstream_id)
        decision = await self._gates.evaluate_ex_post(
            workstream_id,
            workstream.scope,
            workspace=workspace_path,
            approvals=approvals,
        )
        if decision.allow:
            return True
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.FAILED,
            expected_status=WorkstreamStatus.RUNNING,
            error_message=decision.reason,
        )
        await self._route_gate_block(
            workstream_id, decision, expected=WorkstreamStatus.FAILED
        )
        self._stats.failed += 1
        # Leave the workspace intact so a human can inspect the diff.
        return False

    async def _route_gate_block(
        self,
        workstream_id: str,
        decision: GateDecision,
        *,
        expected: WorkstreamStatus,
    ) -> None:
        self._logger.warning(
            "Gates blocked workstream '%s': %s", workstream_id, decision.reason
        )
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=expected,
            error_message=decision.reason,
        )

    async def _handle_completion(
        self,
        workstream_id: str,
        running: RunningWorkstream,
        return_code: int,
    ) -> None:
        """Handle spec-runner process completion."""
        if return_code == 0:
            self._logger.info(
                "Workstream '%s' completed successfully",
                workstream_id,
            )
            await self._handle_success(workstream_id, running.workspace_path)
        else:
            self._logger.warning(
                "Workstream '%s' failed (code %d)",
                workstream_id,
                return_code,
            )
            await self._handle_failure(
                workstream_id,
                f"spec-runner exited with code {return_code}",
            )

    async def _handle_success(
        self,
        workstream_id: str,
        workspace_path: Path,
    ) -> None:
        """Handle successful workstream completion.

        Push branch, create PR, cleanup workspace.
        """
        workstream = await self._db.get_workstream(workstream_id)

        # Gates (WS-006): ex-post guard over the actual diff (catches scope
        # violations and tier escalations the declared scope did not show).
        if not await self._gate_ex_post(workstream_id, workstream, workspace_path):
            return

        # Transition to MERGING
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.MERGING,
            expected_status=WorkstreamStatus.RUNNING,
        )

        # Push branch and create PR
        if self._config.auto_pr:
            try:
                pr_url = self._pr_manager.push_and_create_pr(
                    branch=workstream.branch,
                    title=f"[Maestro] {workstream.title}",
                    body=self._build_pr_body(workstream),
                    base_branch=self._config.base_branch,
                )

                await self._db.update_workstream_status(
                    workstream_id,
                    WorkstreamStatus.PR_CREATED,
                    pr_url=pr_url,
                )

                self._stats.prs_created += 1
                self._logger.info(
                    "Created PR for '%s': %s",
                    workstream_id,
                    pr_url,
                )
            except PRManagerError as e:
                self._logger.warning(
                    "Failed to create PR for '%s': %s",
                    workstream_id,
                    e,
                )
                # Still mark as PR_CREATED (PR may exist). If error_message
                # currently carries an ex-post approval marker (H-6 resume
                # tail), APPEND the note instead of overwriting it: an
                # overwrite would destroy the marker mid-resume, and a
                # later crash before DONE would then full-respawn instead
                # of resuming (the marker clears ONLY on the DONE write).
                note = f"PR creation note: {e}"
                await self._db.update_workstream_status(
                    workstream_id,
                    WorkstreamStatus.PR_CREATED,
                    error_message=preserve_approval_marker(
                        note, workstream.error_message
                    ),
                )

        # Ensure the workstream is at PR_CREATED (both auto_pr paths converge
        # here); auto_pr=False creates no PR, so pass MERGING -> PR_CREATED.
        current = await self._db.get_workstream(workstream_id)
        if current.status == WorkstreamStatus.MERGING:
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.PR_CREATED,
            )

        # Merge the feature branch into base BEFORE marking DONE, so DONE is
        # gated on a successful merge. A conflict/failure routes to
        # NEEDS_REVIEW (a human resolves it; re-running run --all cannot), and
        # a crash mid-merge leaves the workstream pre-DONE for startup recovery.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                self._merge_into_base,
                workstream.branch,
            )
        except GitError as e:
            self._logger.warning(
                "Base merge failed for '%s'; routing to NEEDS_REVIEW: %s",
                workstream_id,
                e,
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.FAILED,
                expected_status=WorkstreamStatus.PR_CREATED,
                error_message=f"Base merge failed: {e}",
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED,
            )
            self._stats.failed += 1
            # Leave the workspace intact so a human can resolve the conflict.
            return

        # Merge succeeded -> DONE. Clear error_message here ONLY: it may
        # still hold an ex-post approval marker (H-6) that recovery needs
        # to survive a crash-tail through MERGING/PR_CREATED (both reset to
        # READY on recovery, and the next run must still see the marker to
        # resume instead of full-respawning).
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.DONE,
            expected_status=WorkstreamStatus.PR_CREATED,
            error_message=None,
        )
        self._stats.completed += 1

        # Cleanup workspace
        self._workspace_mgr.cleanup_workspace(workstream_id)

    async def _handle_failure(
        self,
        workstream_id: str,
        error_message: str,
    ) -> None:
        """Handle workstream failure with retry logic."""
        workstream = await self._db.get_workstream(workstream_id)

        # H-6 position retention (not authority — that lives in
        # gate_approvals): preserve any approval marker already carried in
        # error_message so an ordinary failure overwrite doesn't force a
        # full respawn on the next approved retry.
        preserved = preserve_approval_marker(error_message, workstream.error_message)

        if workstream.can_retry():
            new_count = workstream.retry_count + 1
            self._logger.info(
                "Retrying workstream '%s' (%d/%d)",
                workstream_id,
                new_count,
                workstream.max_retries,
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.FAILED,
                error_message=preserved,
                retry_count=new_count,
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.READY,
                expected_status=WorkstreamStatus.FAILED,
            )
        else:
            self._logger.warning(
                "Workstream '%s' exhausted retries",
                workstream_id,
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.FAILED,
                error_message=preserved,
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED,
            )
            self._stats.failed += 1

    def _build_pr_body(self, workstream: Workstream) -> str:
        """Build PR body from workstream info."""
        scope_str = "\n".join(f"- `{s}`" for s in workstream.scope)
        return (
            f"## Summary\n\n"
            f"{workstream.description}\n\n"
            f"## Scope\n\n"
            f"{scope_str}\n\n"
            f"## Progress\n\n"
            f"{workstream.subtask_progress or 'N/A'}\n\n"
            f"---\n"
            f"🤖 Generated by Maestro Orchestrator"
        )

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        if self._loop is None:
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self) -> None:
        """Handle shutdown signal."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """Cleanup running processes and in-flight generations on shutdown."""
        for _zid, task in list(self._generating.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._generating.clear()

        for zid, running in list(self._running.items()):
            try:
                running.process.terminate()
                await asyncio.sleep(self._shutdown_grace_seconds)
                if running.process.returncode is None:
                    running.process.kill()
                await running.process.wait()
            except OSError as e:
                self._logger.debug(
                    "Failed to terminate process for workstream %s during cleanup: %s",
                    zid,
                    e,
                )

            try:
                await self._db.update_workstream_status(
                    zid,
                    WorkstreamStatus.FAILED,
                    error_message="Orchestrator shutdown",
                )
                await self._db.update_workstream_status(
                    zid,
                    WorkstreamStatus.READY,
                    expected_status=WorkstreamStatus.FAILED,
                )
            except Exception as e:
                self._logger.warning(
                    "Failed to update workstream '%s' during cleanup: %s",
                    zid,
                    e,
                )

        self._running.clear()

        if self._loop:
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(ValueError):
                    self._loop.remove_signal_handler(sig)

        self._loop = None
