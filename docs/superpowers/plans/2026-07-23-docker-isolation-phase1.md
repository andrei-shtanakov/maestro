# Docker Isolation Phase 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Maestro agent tasks (Mode 1) and spec-runner workstreams (Mode 2) inside a local Docker container with a bind-mounted workspace, selectable per entity via `backend: docker`, with durable execution identity, fail-closed recovery, and guaranteed container cleanup — zero runtime regression when no `execution` config is present.

**Architecture:** Compose on top of the Phase-0 execution contract. `LocalBackend` stays the single transport; an injected `Isolator` (`BareIsolator` default, `DockerIsolator` new) rewrites argv/env/mounts. A per-dispatch `BackendResolver` replaces the hardcoded `LocalBackend`. A dedicated `execution_handles` table + an atomic `start_execution` primitive give durable identity; a single-owner `finalize_handle` guarantees cleanup on every terminal path; recovery probes containers by label and fails closed to `NEEDS_REVIEW`.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, aiosqlite, Typer, asyncio subprocess, the `docker` CLI (shelled out, no SDK), pytest (`asyncio_mode = "auto"`), pyrefly, ruff.

**Spec:** `docs/superpowers/specs/2026-07-23-maestro-docker-isolation-phase1-design.md` (read it before starting; section numbers below reference it).

## Global Constraints

- **Package management:** `uv` only. `uv add <pkg>` / `uv run <tool>`. Never `pip`.
- **Checks after every task:** `uv run pytest`, `uv run pyrefly check`, `uv run ruff format .`, `uv run ruff check .` — all clean.
- **Line length:** 88 chars. Type hints on all code. Docstrings on public APIs.
- **No new runtime dependency.** The `docker` CLI is shelled out; there is no `docker` Python package.
- **Zero regression:** with no `execution` config the runtime path is byte-identical to today (the schema migration still runs — that is the only observable change).
- **No `--rm` on execution containers** (recovery must observe exited/dead containers). Helper containers (tool probes) use `--rm` + a unique label.
- **Never mount the Docker socket.** The workspace bind mount is the only project mount.
- **Secret values never appear** in the plan, argv, logs, event log, or DB — only in a `0600` env-file read at `materialize`.
- **`git` workflow:** work on branch `design/docker-isolation-phase1` (already created). No direct commits to `master`. Commit after every task.
- **`_cowork_output/` is dev-only:** never read/resolve it from runtime code. Do not edit neighbor repos (`../proctor/` etc.) — proctor patterns are reimplemented natively, not vendored.

---

## File Structure

**New files:**
- `maestro/execution/isolators.py` — `Isolator` protocol, `BareIsolator`, `DockerIsolator`.
- `maestro/execution/docker_handle.py` — `DockerTaskHandle`.
- `maestro/execution/docker_cli.py` — thin async `docker` CLI wrapper (`RunCmd` injection, `inspect`/`ps`/`rm`/`stop`/`kill`, `--format '{{json .}}'` parsing).
- `maestro/execution/finalize.py` — `FinalizationResult`, `finalize_handle`.
- `maestro/execution/resolver.py` — `BackendResolver`, `ExecutionConfigError`.
- `maestro/execution/exec_config.py` — `ExecutionConfig`, `DockerConfig` Pydantic models + parsing helpers.

**Modified files:**
- `maestro/execution/models.py` — extend `ExecutionRequest` (launch fields); add `PreparedRunPlan`, `PreparedRun`.
- `maestro/execution/local.py` — `LocalBackend` gains an injected isolator; `run()` goes through `prepare`/`materialize`/`transport_ref`/`wrap`.
- `maestro/models.py` — `backend` field on `TaskConfig`/`WorkstreamConfig` + runtime `Task`/`Workstream`; `execution` on `ProjectConfig`/`OrchestratorConfig`.
- `maestro/config.py` — parse the `execution` block for both modes.
- `maestro/database.py` — `execution_handles` table + migration + `backend` columns; `start_execution`, `mark_execution_state`, `get_open_execution_handles`.
- `maestro/scheduler.py` — resolver + launch-identity mint + `start_execution` + single-owner finalize wiring + docker recovery.
- `maestro/orchestrator.py` — same wiring for Mode 2.
- `maestro/recovery.py` — docker probe classification (Mode 1).
- `maestro/CLAUDE.md` — drift note (§15).
- `examples/with-docker.yaml` — new example config.

**Test files:** one per new unit (`tests/test_isolators.py`, `tests/test_docker_handle.py`, `tests/test_docker_cli.py`, `tests/test_finalize.py`, `tests/test_backend_resolver.py`, `tests/test_exec_config.py`, `tests/test_execution_handles.py`, `tests/test_docker_recovery.py`) plus additions to existing scheduler/orchestrator tests and an opt-in `tests/test_docker_integration.py`.

---

# Increment 1a — Seam + resolver + finalize + config (no-op/local, zero regression)

Everything here keeps the local path behavior-compatible; no Docker is spawned.

## Task 1: Extend `ExecutionRequest`; add `PreparedRunPlan` / `PreparedRun`

**Files:**
- Modify: `maestro/execution/models.py`
- Test: `tests/test_execution_models.py` (create)

**Interfaces:**
- Produces: `ExecutionRequest` gains `execution_id: str | None = None`, `entity_kind: Literal["task","workstream"] | None = None`, `attempt: int = 1`, `backend_id: str = "local"`. New models `PreparedRunPlan`, `PreparedRun`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_execution_models.py
from pathlib import Path

from maestro.execution.models import (
    CollectPolicy,
    ExecutionRequest,
    PreparedRun,
    PreparedRunPlan,
)


def _req(**kw) -> ExecutionRequest:
    base = dict(
        run_id="task-1",
        argv=["echo", "hi"],
        workdir=Path("/tmp/wd"),
        log_path=Path("/tmp/wd/log"),
        collect=CollectPolicy(mode="none"),
    )
    base.update(kw)
    return ExecutionRequest(**base)


def test_launch_fields_default_to_local_compatible_values():
    req = _req()
    assert req.execution_id is None
    assert req.entity_kind is None
    assert req.attempt == 1
    assert req.backend_id == "local"


def test_launch_fields_round_trip():
    req = _req(
        execution_id="11111111-1111-4111-8111-111111111111",
        entity_kind="workstream",
        attempt=3,
        backend_id="docker",
    )
    again = ExecutionRequest.model_validate(req.model_dump())
    assert again.entity_kind == "workstream"
    assert again.attempt == 3
    assert again.backend_id == "docker"


def test_prepared_run_plan_defaults():
    plan = PreparedRunPlan(argv=["docker", "run"], env={"A": "1"})
    assert plan.container_name is None
    assert plan.labels == {}
    assert plan.env_file_keys == []
    assert plan.cidfile_path is None
    assert plan.tmp_dir is None
    prepared = PreparedRun(plan=plan)
    assert prepared.env_file is None
    assert prepared.cleanup_paths == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'PreparedRunPlan'`.

- [ ] **Step 3: Add the launch fields and models**

In `maestro/execution/models.py`, add `execution_id`/`entity_kind`/`attempt`/`backend_id` to `ExecutionRequest` (after `required_tools`), and append the two new models. Ensure `Literal` is imported (it already is).

```python
# --- inside class ExecutionRequest, after `required_tools` ---
    execution_id: str | None = None
    entity_kind: Literal["task", "workstream"] | None = None
    attempt: int = 1
    backend_id: str = "local"
```

```python
# --- new models, place after ExecutionRequest ---
class PreparedRunPlan(BaseModel):
    """Deterministic launch plan (no I/O performed to build it)."""

    argv: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    container_name: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    env_file_keys: list[str] = Field(default_factory=list)
    cidfile_path: Path | None = None
    tmp_dir: Path | None = None


class PreparedRun(BaseModel):
    """A plan after its filesystem side effects have been materialized."""

    plan: PreparedRunPlan
    env_file: Path | None = None
    cleanup_paths: list[Path] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execution_models.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/models.py tests/test_execution_models.py
git commit -m "feat(execution): launch fields on ExecutionRequest + PreparedRun models"
```

## Task 2: `Isolator` protocol + `BareIsolator`

**Files:**
- Create: `maestro/execution/isolators.py`
- Test: `tests/test_isolators.py` (create)

**Interfaces:**
- Consumes: `ExecutionRequest`, `PreparedRunPlan`, `PreparedRun` (Task 1); `LocalTaskHandle`, `ExecutionHandleRef`, `TaskHandle`.
- Produces:
  - `class Isolator(Protocol)` with `id: str`, `prepare(req, *, trace_env: Mapping[str,str], host_env: Mapping[str,str]) -> PreparedRunPlan`, `materialize(plan) -> PreparedRun`, `transport_ref(prepared, pid: int) -> str`, `wrap(local, prepared, ref) -> TaskHandle`.
  - `class BareIsolator` with `id = "bare"`, implementing the identity path (reproduces `build_local_env`, `env.py:15-20`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_isolators.py
from pathlib import Path

from maestro.execution.isolators import BareIsolator
from maestro.execution.models import CollectPolicy, ExecutionRequest


def _req(**kw) -> ExecutionRequest:
    base = dict(
        run_id="task-1",
        argv=["claude", "-p", "hi"],
        workdir=Path("/tmp/wd"),
        log_path=Path("/tmp/wd/log"),
        collect=CollectPolicy(mode="none"),
    )
    base.update(kw)
    return ExecutionRequest(**base)


def test_bare_inherit_env_merges_host_then_trace():
    iso = BareIsolator()
    plan = iso.prepare(
        _req(inherit_env=True),
        trace_env={"TRACEPARENT": "tp"},
        host_env={"PATH": "/bin", "TRACEPARENT": "old"},
    )
    assert plan.argv == ["claude", "-p", "hi"]
    assert plan.env == {"PATH": "/bin", "TRACEPARENT": "tp"}
    assert plan.container_name is None


def test_bare_allowlist_env_when_not_inheriting():
    iso = BareIsolator()
    plan = iso.prepare(
        _req(secret_env=["ANTHROPIC_API_KEY", "MISSING"], env={"X": "1"}),
        trace_env={"TRACEPARENT": "tp"},
        host_env={"ANTHROPIC_API_KEY": "sk-abc", "PATH": "/bin"},
    )
    # host PATH is NOT inherited; only allowlisted secret + explicit env + trace
    assert plan.env == {"ANTHROPIC_API_KEY": "sk-abc", "X": "1", "TRACEPARENT": "tp"}


def test_bare_materialize_is_noop_and_transport_ref_is_local_pid():
    iso = BareIsolator()
    plan = iso.prepare(_req(), trace_env={}, host_env={})
    prepared = iso.materialize(plan)
    assert prepared.env_file is None
    assert prepared.cleanup_paths == []
    assert iso.transport_ref(prepared, 4242) == "local_pid:4242"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_isolators.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.isolators`.

- [ ] **Step 3: Implement the protocol and `BareIsolator`**

```python
# maestro/execution/isolators.py
"""Isolators: compose with LocalBackend to rewrite argv/env/mounts.

`prepare` is deterministic w.r.t. its arguments (no os.environ/child_env reads);
`materialize` performs the filesystem side effects right before spawn.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from maestro.execution.backend import TaskHandle
from maestro.execution.local import LocalTaskHandle
from maestro.execution.models import (
    ExecutionHandleRef,
    ExecutionRequest,
    PreparedRun,
    PreparedRunPlan,
)


@runtime_checkable
class Isolator(Protocol):
    """Rewrites a request's argv/env and wraps the resulting handle."""

    id: str

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan: ...

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun: ...

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str: ...

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle: ...


class BareIsolator:
    """Identity isolator: reproduces build_local_env exactly (env.py:15-20)."""

    id = "bare"

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan:
        if req.inherit_env:
            env = {**host_env, **trace_env}
        else:
            allowed = {k: host_env[k] for k in req.secret_env if k in host_env}
            env = {**allowed, **req.env, **trace_env}
        return PreparedRunPlan(argv=list(req.argv), env=env)

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun:
        return PreparedRun(plan=plan)

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:
        return f"local_pid:{pid}"

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle:
        return local
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_isolators.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/isolators.py tests/test_isolators.py
git commit -m "feat(execution): Isolator protocol + BareIsolator (identity path)"
```

## Task 3: `LocalBackend` uses the isolator seam

**Files:**
- Modify: `maestro/execution/local.py`
- Test: `tests/test_local_backend.py` (create — golden env/argv parity)

**Interfaces:**
- Consumes: `Isolator`, `BareIsolator` (Task 2).
- Produces: `LocalBackend(isolator: Isolator | None = None)` — default `BareIsolator()`. `run()` now calls `isolator.prepare(req, trace_env=child_env(), host_env=dict(os.environ))` → `materialize` → spawn `prepared.plan.argv` with `prepared.plan.env` → build ref via `isolator.transport_ref(...)` → return `isolator.wrap(...)`.

- [ ] **Step 1: Write the failing test** (locks env/argv parity with the old path)

```python
# tests/test_local_backend.py
import os
from pathlib import Path

import pytest

from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest


@pytest.mark.anyio
async def test_local_backend_runs_and_reaps(tmp_path: Path):
    log = tmp_path / "log.txt"
    req = ExecutionRequest(
        run_id="t1",
        argv=["python", "-c", "print('hello-local')"],
        workdir=tmp_path,
        log_path=log,
        collect=CollectPolicy(mode="none"),
    )
    handle = await LocalBackend().run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    assert "hello-local" in log.read_text()


@pytest.mark.anyio
async def test_local_backend_passes_allowlisted_secret_not_full_env(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    monkeypatch.setenv("MY_OTHER", "leak")
    log = tmp_path / "log.txt"
    req = ExecutionRequest(
        run_id="t2",
        argv=["python", "-c", "import os;print(os.environ.get('MY_SECRET'),os.environ.get('MY_OTHER'))"],
        workdir=tmp_path,
        log_path=log,
        secret_env=["MY_SECRET"],
        inherit_env=False,
        collect=CollectPolicy(mode="none"),
    )
    handle = await LocalBackend().run(req)
    await handle.wait()
    # allowlisted secret present, non-allowlisted host var absent
    assert "s3cr3t None" in log.read_text()


@pytest.mark.anyio
async def test_spawn_failure_cleans_materialized_files(tmp_path: Path):
    """Must-have #3: a spawn failure removes files the isolator materialized."""
    from maestro.execution.models import PreparedRun, PreparedRunPlan

    leftover = tmp_path / "env"
    leftover.write_text("SECRET=1")

    class _FakeIso:
        id = "fake"

        def prepare(self, req, *, trace_env, host_env):
            return PreparedRunPlan(argv=["/nonexistent/binary-xyz"], env={})

        def materialize(self, plan):
            return PreparedRun(plan=plan, cleanup_paths=[leftover])

        def transport_ref(self, prepared, pid):
            return f"local_pid:{pid}"

        def wrap(self, local, prepared, ref):
            return local

    req = ExecutionRequest(
        run_id="t3",
        argv=["ignored"],
        workdir=tmp_path,
        log_path=tmp_path / "log",
        collect=CollectPolicy(mode="none"),
    )
    with pytest.raises(FileNotFoundError):
        await LocalBackend(_FakeIso()).run(req)
    assert not leftover.exists()   # spawn-failure path unlinked it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_local_backend.py -v`
Expected: the second test FAILS today (the current `build_local_env` at `inherit_env=False` already allowlists, so it may pass; if it passes, that confirms parity). The first should pass. If both pass pre-change, proceed — Step 3 is a refactor that must keep them green.

- [ ] **Step 3: Refactor `LocalBackend.run` through the isolator**

Add an `__init__` and rewrite `run` to route through the isolator. Keep `LocalTaskHandle`, `_decode_tail`, `probe` unchanged. Import `child_env` and `BareIsolator`.

```python
# top of maestro/execution/local.py — add imports
from collections.abc import Mapping  # noqa: F401 (used by type hints below)
from maestro._vendor.obs import child_env
# NOTE: import Isolator lazily to avoid a cycle (isolators.py imports local.py).
```

```python
# replace `class LocalBackend:` header + run(), keep healthcheck/can_run/probe
class LocalBackend:
    """Runs an ExecutionRequest as a local asyncio subprocess."""

    id = "local"

    def __init__(self, isolator=None) -> None:
        # Default BareIsolator; imported lazily to break the import cycle.
        if isolator is None:
            from maestro.execution.isolators import BareIsolator

            isolator = BareIsolator()
        self._isolator = isolator

    async def run(self, req: ExecutionRequest) -> TaskHandle:
        plan = self._isolator.prepare(
            req, trace_env=dict(child_env()), host_env=dict(os.environ)
        )
        prepared = self._isolator.materialize(plan)
        argv = prepared.plan.argv
        env = prepared.plan.env
        log_fd: int | None = None
        if req.capture_output:
            stdout = asyncio.subprocess.PIPE
            stderr = asyncio.subprocess.PIPE
        else:
            log_fd = os.open(
                str(req.log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            )
            stdout = log_fd
            stderr = asyncio.subprocess.STDOUT
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=req.workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE if req.stdin is not None else None,
                stdout=stdout,
                stderr=stderr,
            )
        except BaseException:
            if log_fd is not None:
                os.close(log_fd)
            # Clean any files the isolator created before the spawn failed.
            _cleanup_prepared(prepared)
            raise
        if log_fd is not None:
            os.close(log_fd)
            log_fd = None
        if req.stdin is not None and proc.stdin is not None:
            proc.stdin.write(req.stdin.encode("utf-8"))
            proc.stdin.close()
        ref = ExecutionHandleRef(
            backend_id=req.backend_id,
            run_id=req.run_id,
            transport_ref=self._isolator.transport_ref(prepared, proc.pid),
            started_at=datetime.now(UTC),
        )
        local_handle = LocalTaskHandle(proc, req, log_fd, ref)
        return self._isolator.wrap(local_handle, prepared, ref)

    # healthcheck / can_run / probe unchanged (keep existing bodies)
```

Add a module-level helper used above (place near `_decode_tail`):

```python
def _cleanup_prepared(prepared) -> None:
    """Best-effort removal of files an isolator materialized (spawn-failure path)."""
    import contextlib
    import shutil

    for path in getattr(prepared, "cleanup_paths", []) or []:
        with contextlib.suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
```

Note: `req.backend_id` now feeds `ExecutionHandleRef.backend_id` (was `self.id`). For local requests `backend_id` defaults to `"local"`, preserving today's value.

- [ ] **Step 4: Run tests to verify parity**

Run: `uv run pytest tests/test_local_backend.py tests/test_execution*.py -v`
Expected: PASS. Then run the full suite to prove no regression: `uv run pytest`.
Expected: all green (the Phase-0 execution tests still pass unchanged).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/local.py tests/test_local_backend.py
git commit -m "refactor(execution): route LocalBackend through an injected isolator"
```

## Task 4: `FinalizationResult` + `finalize_handle`

**Files:**
- Create: `maestro/execution/finalize.py`
- Test: `tests/test_finalize.py` (create)

**Interfaces:**
- Consumes: `TaskHandle`, `ExecutionResult`.
- Produces: `@dataclass FinalizationResult(execution: ExecutionResult, collect_error: str | None, cleanup_error: str | None)` with `.cleaned` property; `async def finalize_handle(handle: TaskHandle) -> FinalizationResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_finalize.py
import pytest

from maestro.execution.finalize import FinalizationResult, finalize_handle
from maestro.execution.models import ExecutionResult
from pathlib import Path


class _FakeHandle:
    def __init__(self, *, collect_raises=False, cleanup_raises=False):
        self._collect_raises = collect_raises
        self._cleanup_raises = cleanup_raises
        self.cleaned_called = False

    async def wait(self) -> ExecutionResult:
        return ExecutionResult(exit_code=0, output_log_path=Path("/tmp/log"))

    async def collect(self):
        if self._collect_raises:
            raise RuntimeError("collect boom")

    async def cleanup(self):
        self.cleaned_called = True
        if self._cleanup_raises:
            raise RuntimeError("rm boom")


@pytest.mark.anyio
async def test_finalize_success_is_cleaned_and_keeps_exit_code():
    fin = await finalize_handle(_FakeHandle())
    assert fin.execution.exit_code == 0
    assert fin.cleaned is True
    assert fin.collect_error is None


@pytest.mark.anyio
async def test_collect_error_recorded_not_fatal_exit_code_untouched():
    fin = await finalize_handle(_FakeHandle(collect_raises=True))
    assert fin.execution.exit_code == 0        # business result untouched
    assert fin.collect_error is not None and "collect boom" in fin.collect_error
    assert fin.cleaned is True                  # cleanup still ran


@pytest.mark.anyio
async def test_cleanup_error_marks_not_cleaned():
    handle = _FakeHandle(cleanup_raises=True)
    fin = await finalize_handle(handle)
    assert handle.cleaned_called is True
    assert fin.cleaned is False
    assert "rm boom" in (fin.cleanup_error or "")
    assert fin.execution.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_finalize.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.finalize`.

- [ ] **Step 3: Implement `finalize_handle`**

```python
# maestro/execution/finalize.py
"""Single-owner finalization: reap, collect, cleanup — as a structured result.

The monitor (not this helper) drives execution_handles state and never lets a
resource-cleanup fault rewrite the agent's business exit code.
"""

from dataclasses import dataclass

from maestro.execution.backend import TaskHandle
from maestro.execution.models import ExecutionResult


@dataclass
class FinalizationResult:
    """Outcome of finalizing a handle."""

    execution: ExecutionResult
    collect_error: str | None = None
    cleanup_error: str | None = None

    @property
    def cleaned(self) -> bool:
        return self.cleanup_error is None


async def finalize_handle(handle: TaskHandle) -> FinalizationResult:
    """Reap the handle, then collect + cleanup, capturing (not raising) faults."""
    execution = await handle.wait()
    collect_error: str | None = None
    cleanup_error: str | None = None
    try:
        await handle.collect()
    except Exception as e:  # noqa: BLE001 — collect must not hide the result
        collect_error = str(e)
    try:
        await handle.cleanup()
    except Exception as e:  # noqa: BLE001 — cleanup fault ≠ execution failure
        cleanup_error = str(e)
    return FinalizationResult(execution, collect_error, cleanup_error)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_finalize.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/finalize.py tests/test_finalize.py
git commit -m "feat(execution): FinalizationResult + finalize_handle helper"
```

## Task 5: Single-owner finalize wiring in the scheduler monitor

**Files:**
- Modify: `maestro/execution/finalize.py` (add `ensure_finalize_task`)
- Modify: `maestro/scheduler.py:158-172` (`RunningTask`), `maestro/scheduler.py:1196-1290` (`_monitor_running_tasks`, `_handle_task_completion`)
- Test: `tests/test_finalize.py` (extend)

**Interfaces:**
- Consumes: `finalize_handle`, `FinalizationResult` (Task 4).
- Produces: `def ensure_finalize_task(running) -> asyncio.Task` — creates the finalize task once and stores it on `running.finalize_task`; `RunningTask.finalize_task: asyncio.Task | None = None`.

- [ ] **Step 1: Write the failing test** (one finalize task per entity)

```python
# append to tests/test_finalize.py
import asyncio
from dataclasses import dataclass, field

from maestro.execution.finalize import ensure_finalize_task


@dataclass
class _Running:
    handle: object
    finalize_task: asyncio.Task | None = None


@pytest.mark.anyio
async def test_ensure_finalize_task_created_once():
    running = _Running(handle=_FakeHandle())
    t1 = ensure_finalize_task(running)
    t2 = ensure_finalize_task(running)
    assert t1 is t2                       # exactly one task per entity
    fin = await asyncio.shield(t1)
    assert fin.cleaned is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_finalize.py::test_ensure_finalize_task_created_once -v`
Expected: FAIL with `ImportError: cannot import name 'ensure_finalize_task'`.

- [ ] **Step 3: Add `ensure_finalize_task` and wire the monitor**

Append to `maestro/execution/finalize.py`:

```python
import asyncio
from typing import Protocol


class _Finalizable(Protocol):
    handle: TaskHandle
    finalize_task: "asyncio.Task[FinalizationResult] | None"


def ensure_finalize_task(running: _Finalizable) -> "asyncio.Task[FinalizationResult]":
    """Create the single finalization task for a running entity (idempotent)."""
    if running.finalize_task is None:
        running.finalize_task = asyncio.create_task(finalize_handle(running.handle))
    return running.finalize_task
```

In `maestro/scheduler.py`, add the field to `RunningTask` (after `log_file`):

```python
    finalize_task: "asyncio.Task | None" = None
```

Import at the top of `scheduler.py`: `from maestro.execution.finalize import ensure_finalize_task`.

Rewrite the completion branch of `_monitor_running_tasks` (currently `scheduler.py:1202-1206`) so finalization happens exactly once before dispatch:

```python
            return_code = running_task.handle.poll()

            if return_code is not None:
                fin = await asyncio.shield(ensure_finalize_task(running_task))
                if fin.collect_error or fin.cleanup_error:
                    _obs_log.warning(
                        "execution.finalize.resource_fault",
                        task_id=task_id,
                        collect_error=fin.collect_error,
                        cleanup_error=fin.cleanup_error,
                    )
                await self._handle_task_completion(
                    task_id, running_task, fin.execution.exit_code
                )
                completed.append(task_id)
            else:
                elapsed = datetime.now(UTC) - running_task.started_at
                timeout_seconds = running_task.task.timeout_minutes * 60
                if elapsed.total_seconds() > timeout_seconds:
                    await running_task.handle.terminate(grace_seconds=10.0)
                    await asyncio.shield(ensure_finalize_task(running_task))
                    await self._handle_task_timeout(task_id, running_task)
                    completed.append(task_id)
```

`_handle_task_completion` already takes `return_code: int` — `fin.execution.exit_code` is `int | None`; guard its one comparison (`if return_code == 0`) is already `== 0`, and `None == 0` is `False`, so a timeout/None routes to the failure branch, which is correct. No signature change needed.

Note: `_handle_task_timeout` may already call `terminate`/`kill`; that stays idempotent. The monitor calling `terminate` + finalize first guarantees the container (Increment 1b) is stopped and removed even though `_handle_task_timeout` only transitions status.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_finalize.py -v && uv run pytest -k scheduler -v`
Expected: PASS; existing scheduler tests stay green (local finalize is a near-no-op: `wait()` after exit returns at once, `collect`/`cleanup` are no-ops).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/finalize.py maestro/scheduler.py tests/test_finalize.py
git commit -m "feat(scheduler): single-owner finalize on every terminal path"
```

## Task 6: `ExecutionConfig` / `DockerConfig` models + `backend` fields + parsing

**Files:**
- Create: `maestro/execution/exec_config.py`
- Modify: `maestro/models.py` (`TaskConfig`, `WorkstreamConfig`, `Task`, `Workstream`, `ProjectConfig`, `OrchestratorConfig`)
- Modify: `maestro/config.py` (parse `execution` for both modes)
- Test: `tests/test_exec_config.py` (create)

**Interfaces:**
- Produces:
  - `class DockerConfig(BaseModel)`: `image: str`, `network: str = "none"`, `memory: str | None = None`, `cpus: str | None = None`, `user: str | None = None`, `secret_env: list[str] = []`.
  - `class ExecutionConfig(BaseModel)`: `default_backend: str = "local"`, `docker: DockerConfig | None = None`.
  - `TaskConfig.backend: str | None = None`, `WorkstreamConfig.backend: str | None = None`, and the same on runtime `Task`/`Workstream`.
  - `ProjectConfig.execution: ExecutionConfig | None`, `OrchestratorConfig.execution: ExecutionConfig | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exec_config.py
import pytest
from pydantic import ValidationError

from maestro.execution.exec_config import DockerConfig, ExecutionConfig


def test_defaults_local_no_docker():
    cfg = ExecutionConfig()
    assert cfg.default_backend == "local"
    assert cfg.docker is None


def test_docker_config_network_defaults_none():
    d = DockerConfig(image="maestro-runner:x")
    assert d.network == "none"
    assert d.secret_env == []


def test_gh_denylist_rejected_in_secret_env():
    with pytest.raises(ValidationError):
        DockerConfig(image="i", secret_env=["ANTHROPIC_API_KEY", "GH_TOKEN"])
    with pytest.raises(ValidationError):
        DockerConfig(image="i", secret_env=["GH_FOO"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exec_config.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.exec_config`.

- [ ] **Step 3: Implement the config models**

```python
# maestro/execution/exec_config.py
"""Execution-backend config models (the narrow Phase-1 `execution` block)."""

from pydantic import BaseModel, Field, field_validator

_GH_DENYLIST_EXACT = {"GH_TOKEN", "GITHUB_TOKEN"}


def _is_denylisted(name: str) -> bool:
    return name in _GH_DENYLIST_EXACT or name.startswith("GH_")


class DockerConfig(BaseModel):
    """Local Docker isolator configuration."""

    image: str
    network: str = "none"
    memory: str | None = None
    cpus: str | None = None
    user: str | None = None
    secret_env: list[str] = Field(default_factory=list)

    @field_validator("secret_env")
    @classmethod
    def _reject_gh(cls, value: list[str]) -> list[str]:
        bad = [n for n in value if _is_denylisted(n)]
        if bad:
            msg = f"secret_env may not carry GitHub credentials: {bad}"
            raise ValueError(msg)
        return value


class ExecutionConfig(BaseModel):
    """The `execution:` block; absent → local + bare, old behavior."""

    default_backend: str = "local"
    docker: DockerConfig | None = None
```

- [ ] **Step 4: Add `backend` fields and `execution` to the root configs**

In `maestro/models.py`:
- Add `backend: str | None = Field(default=None, description="Execution backend name (local|docker); None → default_backend")` to `TaskConfig` (near other optional fields) and to `WorkstreamConfig`.
- Add the same `backend: str | None = None` to runtime `Task` and `Workstream`.
- In `Task.from_config` / `Workstream.from_config` (wherever config → runtime happens), copy `backend` through.
- Import `ExecutionConfig` and add `execution: ExecutionConfig | None = Field(default=None, description="Execution backends")` to `ProjectConfig` and `OrchestratorConfig` (mirror the existing `arbiter` field at `models.py:793` / `:1441`).

In `maestro/config.py`, ensure the loaders pass the `execution` mapping into `ProjectConfig`/`OrchestratorConfig` (Pydantic parses the nested dict automatically once the field exists; verify env-var substitution runs on it like the rest of the config).

Add a round-trip test:

```python
# append to tests/test_exec_config.py
from maestro.models import ProjectConfig, TaskConfig


def test_project_config_execution_round_trip():
    cfg = ProjectConfig.model_validate(
        {
            "tasks": [{"id": "t", "prompt": "p", "backend": "docker"}],
            "execution": {
                "default_backend": "local",
                "docker": {"image": "maestro-runner:x", "secret_env": ["ANTHROPIC_API_KEY"]},
            },
        }
    )
    assert cfg.execution is not None
    assert cfg.execution.docker.image == "maestro-runner:x"
    assert cfg.tasks[0].backend == "docker"
```

(Adjust the minimal `tasks[0]` dict to whatever `TaskConfig` requires — check `TaskConfig` at `models.py:412` for required fields and fill them.)

- [ ] **Step 5: Run tests + full checks + commit**

Run: `uv run pytest tests/test_exec_config.py -v && uv run pytest`
Expected: PASS; existing config/model tests green.

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/exec_config.py maestro/models.py maestro/config.py tests/test_exec_config.py
git commit -m "feat(config): execution block + per-entity backend field (both modes)"
```

## Task 7: `BackendResolver` (local only + fail-fast rules)

**Files:**
- Create: `maestro/execution/resolver.py`
- Test: `tests/test_backend_resolver.py` (create)

**Interfaces:**
- Consumes: `ExecutionConfig`, `LocalBackend`, `BareIsolator`.
- Produces: `class ExecutionConfigError(Exception)`; `class BackendResolver` with `__init__(self, execution: ExecutionConfig | None)`, `resolve(self, name: str | None) -> ExecutionBackend`, `default_name -> str`. In 1a it constructs only `local`; `docker` raises `ExecutionConfigError` unless `execution.docker` is set (the actual docker backend lands in Task 13, where `resolve("docker")` starts returning a real backend).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend_resolver.py
import pytest

from maestro.execution.exec_config import DockerConfig, ExecutionConfig
from maestro.execution.local import LocalBackend
from maestro.execution.resolver import BackendResolver, ExecutionConfigError


def test_no_execution_config_resolves_local():
    r = BackendResolver(None)
    assert r.default_name == "local"
    assert isinstance(r.resolve(None), LocalBackend)
    assert isinstance(r.resolve("local"), LocalBackend)


def test_unknown_backend_name_fails_fast():
    r = BackendResolver(ExecutionConfig())
    with pytest.raises(ExecutionConfigError):
        r.resolve("gpu-box")


def test_docker_without_docker_config_fails_fast():
    r = BackendResolver(ExecutionConfig(default_backend="local"))
    with pytest.raises(ExecutionConfigError):
        r.resolve("docker")


def test_local_instance_is_cached():
    r = BackendResolver(None)
    assert r.resolve("local") is r.resolve("local")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backend_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.resolver`.

- [ ] **Step 3: Implement the resolver (local path; docker stub raises until Task 13)**

```python
# maestro/execution/resolver.py
"""Per-dispatch backend resolution. Fail-fast; never falls back to local."""

from maestro.execution.backend import ExecutionBackend
from maestro.execution.exec_config import ExecutionConfig
from maestro.execution.local import LocalBackend


class ExecutionConfigError(Exception):
    """Raised for an unusable execution config (unknown/mis-specified backend)."""


class BackendResolver:
    """Resolves a backend name to an ExecutionBackend, caching instances."""

    def __init__(self, execution: ExecutionConfig | None) -> None:
        self._execution = execution or ExecutionConfig()
        self._cache: dict[str, ExecutionBackend] = {}

    @property
    def default_name(self) -> str:
        return self._execution.default_backend

    def resolve(self, name: str | None) -> ExecutionBackend:
        backend_name = name or self._execution.default_backend
        if backend_name in self._cache:
            return self._cache[backend_name]
        backend = self._build(backend_name)
        self._cache[backend_name] = backend
        return backend

    def _build(self, name: str) -> ExecutionBackend:
        if name == "local":
            return LocalBackend()
        if name == "docker":
            if self._execution.docker is None:
                raise ExecutionConfigError(
                    "backend 'docker' selected but no execution.docker config"
                )
            # Real docker backend is wired in Task 13.
            raise ExecutionConfigError("docker backend not yet available (Task 13)")
        raise ExecutionConfigError(f"unknown backend '{name}'")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backend_resolver.py -v`
Expected: PASS (the docker-config-present case is added in Task 13).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/resolver.py tests/test_backend_resolver.py
git commit -m "feat(execution): BackendResolver with fail-fast rules (local path)"
```

## Task 8: Wire the resolver into scheduler + orchestrator dispatch

**Files:**
- Modify: `maestro/scheduler.py:264` (`self._backend = LocalBackend()` → resolver), dispatch site `:1059-1090`
- Modify: `maestro/orchestrator.py:194` (`self._backend = LocalBackend()` → resolver), dispatch site `:729-739`
- Test: `tests/test_backend_resolver.py` (extend with a dispatch-selection test)

**Interfaces:**
- Consumes: `BackendResolver` (Task 7); `entity.backend` (Task 6).
- Produces: both loops resolve `backend = self._backends.resolve(entity.backend)` per dispatch; `req.backend_id` set to the resolved name.

- [ ] **Step 1: Write the failing test** (selection precedence)

```python
# append to tests/test_backend_resolver.py
def test_resolve_uses_entity_backend_then_default():
    r = BackendResolver(ExecutionConfig(default_backend="local"))
    # entity backend None → default_name
    assert r.resolve(None).id == "local"
    # explicit local
    assert r.resolve("local").id == "local"
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `uv run pytest tests/test_backend_resolver.py::test_resolve_uses_entity_backend_then_default -v`
Expected: PASS (resolver already supports this) — this test documents the dispatch contract the wiring must honor.

- [ ] **Step 3: Replace the hardcoded backends**

In `maestro/scheduler.py` `__init__`, replace `self._backend = LocalBackend()` with:

```python
        self._backends = BackendResolver(
            getattr(self._project_config, "execution", None)
        )
```

At the dispatch site (`_spawn_task`, around `:1059`), select per task and set `backend_id` on the request before `can_run`/`run`:

```python
        backend = self._backends.resolve(task.backend)
        request = request.model_copy(update={"backend_id": backend.id})
        cap = await backend.can_run(request)
        # ... existing not-available handling ...
        handle = await backend.run(request)
```

Apply the identical change in `maestro/orchestrator.py` (`self._backends = BackendResolver(getattr(self._config, "execution", None))` in `__init__`; resolve per workstream at `:729-739`). Import `BackendResolver` in both files. If `self._project_config`/`self._config` attribute names differ, use the actual field holding the loaded root config.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest`
Expected: all green — with no `execution` config, `resolve(None)` returns `LocalBackend`, so behavior is unchanged.

- [ ] **Step 5: Full checks + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/scheduler.py maestro/orchestrator.py tests/test_backend_resolver.py
git commit -m "feat(execution): per-dispatch backend resolution in both modes"
```

**Increment 1a done:** the seam, resolver, config, and single-owner finalize are in place; the local path is behavior-compatible; no Docker code runs yet.

---

# Increment 1b — DockerIsolator + DockerTaskHandle + secrets/mounts

## Task 9: `docker_cli` — thin async CLI wrapper (daemon-free testable)

**Files:**
- Create: `maestro/execution/docker_cli.py`
- Test: `tests/test_docker_cli.py` (create)

**Interfaces:**
- Produces: `RunCmd = Callable[[list[str], float | None], Awaitable[tuple[int, str, str]]]`; `class DockerCli` with async `version_ok()`, `image_exists(image)`, `inspect(name) -> dict | None`, `ps_ids_by_label(key, value) -> list[str]`, `stop(name, timeout)`, `kill(name)`, `rm(name)`. Built on an injected `run_cmd` so tests need no daemon (proctor prior-art pattern, reimplemented natively).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docker_cli.py
import json

import pytest

from maestro.execution.docker_cli import DockerCli


class _FakeDocker:
    """Records argv and returns scripted (rc, out, err)."""

    def __init__(self, script):
        self.script = script          # list[tuple[rc, out, err]]
        self.calls: list[list[str]] = []
        self._i = 0

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        rc, out, err = self.script[self._i]
        self._i += 1
        return rc, out, err


@pytest.mark.anyio
async def test_inspect_returns_none_when_absent():
    fake = _FakeDocker([(1, "", "No such object: maestro-x")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.inspect("maestro-x") is None
    assert fake.calls[0][:2] == ["docker", "inspect"]


@pytest.mark.anyio
async def test_inspect_parses_json():
    payload = json.dumps({"Id": "abc", "Config": {"Labels": {"maestro.execution_id": "e1"}}})
    cli = DockerCli(run_cmd=_FakeDocker([(0, payload, "")]))
    got = await cli.inspect("maestro-e1")
    assert got["Config"]["Labels"]["maestro.execution_id"] == "e1"


@pytest.mark.anyio
async def test_ps_ids_by_label_splits_lines():
    cli = DockerCli(run_cmd=_FakeDocker([(0, "id1\nid2\n", "")]))
    ids = await cli.ps_ids_by_label("maestro.execution_id", "e1")
    assert ids == ["id1", "id2"]


@pytest.mark.anyio
async def test_rm_is_forced():
    fake = _FakeDocker([(0, "", "")])
    await DockerCli(run_cmd=fake).rm("maestro-e1")
    assert fake.calls[0] == ["docker", "rm", "-f", "maestro-e1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docker_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.docker_cli`.

- [ ] **Step 3: Implement `DockerCli`**

```python
# maestro/execution/docker_cli.py
"""Async wrapper over the `docker` CLI. All ops shell out via an injected
run_cmd so unit tests need no daemon. inspect reads `--format '{{json .}}'`
only — never scraped human text.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

RunCmd = Callable[[list[str], "float | None"], Awaitable[tuple[int, str, str]]]


def _default_run_cmd() -> RunCmd:
    async def run_cmd(argv: list[str], timeout: float | None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    return run_cmd


class DockerCli:
    """Minimal docker operations Maestro needs."""

    def __init__(
        self,
        binary: str = "docker",
        run_cmd: RunCmd | None = None,
        op_timeout: float = 30.0,
    ) -> None:
        self._binary = binary
        self._run = run_cmd or _default_run_cmd()
        self._op_timeout = op_timeout

    async def version_ok(self) -> bool:
        rc, _, _ = await self._run([self._binary, "version"], self._op_timeout)
        return rc == 0

    async def image_exists(self, image: str) -> bool:
        rc, _, _ = await self._run(
            [self._binary, "image", "inspect", image], self._op_timeout
        )
        return rc == 0

    async def inspect(self, name: str) -> dict[str, Any] | None:
        rc, out, _ = await self._run(
            [self._binary, "inspect", "--format", "{{json .}}", name],
            self._op_timeout,
        )
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        # -a: include running, exited, dead, paused, restarting.
        rc, out, err = await self._run(
            [self._binary, "ps", "-a", "-q", "--filter", f"label={key}={value}"],
            self._op_timeout,
        )
        if rc != 0:
            raise RuntimeError(f"docker ps failed: {err.strip()}")
        return [line for line in out.splitlines() if line.strip()]

    async def stop(self, name: str, timeout: float) -> None:
        secs = max(1, int(timeout))
        await self._run([self._binary, "stop", "-t", str(secs), name], secs + 10.0)

    async def kill(self, name: str) -> None:
        await self._run([self._binary, "kill", name], self._op_timeout)

    async def rm(self, name: str) -> None:
        await self._run([self._binary, "rm", "-f", name], self._op_timeout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_docker_cli.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/docker_cli.py tests/test_docker_cli.py
git commit -m "feat(execution): DockerCli async wrapper (injected run_cmd, json inspect)"
```

## Task 10: `DockerIsolator.prepare` (pure argv/mounts/labels/env split)

**Files:**
- Modify: `maestro/execution/isolators.py` (add `DockerIsolator`, `prepare` only)
- Test: `tests/test_isolators.py` (extend)

**Interfaces:**
- Consumes: `DockerConfig` (Task 6), `PreparedRunPlan`, `ExecutionRequest`.
- Produces: `class DockerIsolator` with `id = "docker"`, `__init__(self, cfg: DockerConfig, docker: DockerCli | None = None)`, and a `prepare` that requires `req.execution_id` (raises if `None`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_isolators.py
import pytest as _pytest

from maestro.execution.exec_config import DockerConfig
from maestro.execution.isolators import DockerIsolator


def _docker_iso(**cfg_kw):
    cfg = DockerConfig(image="maestro-runner:x", **cfg_kw)
    return DockerIsolator(cfg)


def test_docker_prepare_requires_execution_id():
    with _pytest.raises(ValueError):
        _docker_iso().prepare(_req(), trace_env={}, host_env={})


def test_docker_prepare_builds_run_argv_with_mounts_labels():
    iso = _docker_iso(network="none", memory="8g", cpus="2", user="1000:1000")
    req = _req(
        execution_id="e-123",
        entity_kind="task",
        attempt=2,
        argv=["claude", "-p", "hi"],
    )
    plan = iso.prepare(req, trace_env={"TRACEPARENT": "tp"}, host_env={})
    assert plan.container_name == "maestro-e-123"
    assert plan.argv[0:2] == ["docker", "run"]
    assert "--name" in plan.argv and "maestro-e-123" in plan.argv
    # workspace bind mount is the only -v; docker socket never mounted
    assert plan.argv.count("-v") == 1
    assert f"{req.workdir}:/work" in plan.argv
    assert "-w" in plan.argv and "/work" in plan.argv
    assert "--network" in plan.argv and "none" in plan.argv
    assert "--memory" in plan.argv and "8g" in plan.argv
    assert "--user" in plan.argv and "1000:1000" in plan.argv
    assert "--rm" not in plan.argv                     # execution containers never use --rm
    # identity labels
    joined = " ".join(plan.argv)
    assert "maestro.execution_id=e-123" in joined
    assert "maestro.entity_kind=task" in joined
    assert "maestro.attempt=2" in joined
    # trace env inlined via -e; original argv preserved at the tail
    assert "TRACEPARENT=tp" in joined
    assert plan.argv[-3:] == ["claude", "-p", "hi"]
    # secret names planned but no --env-file yet when there are no secrets
    assert plan.env_file_keys == []


def test_docker_prepare_plans_env_file_for_secrets():
    iso = _docker_iso(secret_env=["ANTHROPIC_API_KEY"])
    plan = iso.prepare(
        _req(execution_id="e-9"),
        trace_env={},
        host_env={"ANTHROPIC_API_KEY": "sk"},
    )
    assert plan.env_file_keys == ["ANTHROPIC_API_KEY"]
    assert "--env-file" in plan.argv
    # the value never appears in argv
    assert "sk" not in " ".join(plan.argv)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_isolators.py -k docker -v`
Expected: FAIL with `ImportError: cannot import name 'DockerIsolator'`.

- [ ] **Step 3: Implement `DockerIsolator.prepare`**

Add to `maestro/execution/isolators.py` (import `DockerConfig`, `DockerCli`, `Path`):

```python
from pathlib import Path

from maestro.execution.docker_cli import DockerCli
from maestro.execution.exec_config import DockerConfig


class DockerIsolator:
    """Runs argv inside a local Docker container with a bind-mounted workspace."""

    id = "docker"

    def __init__(self, cfg: DockerConfig, docker: DockerCli | None = None) -> None:
        self._cfg = cfg
        self._docker = docker or DockerCli()

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan:
        if req.execution_id is None:
            raise ValueError("DockerIsolator requires req.execution_id")
        name = f"maestro-{req.execution_id}"
        base = Path(host_env.get("TMPDIR", "/tmp"))
        tmp_dir = base / f"maestro-exec-{req.execution_id}"
        cidfile = tmp_dir / "cid"
        env_file = tmp_dir / "env"

        # secret NAMES that actually exist on the host (values read in materialize)
        secret_keys = [k for k in self._cfg.secret_env if k in host_env]
        labels = {
            "maestro.execution_id": req.execution_id,
            "maestro.entity_kind": req.entity_kind or "task",
            "maestro.entity_id": req.run_id,
            "maestro.attempt": str(req.attempt),
            "maestro.backend_id": "docker",
        }
        argv: list[str] = [
            "docker", "run",
            "--name", name,
            "--cidfile", str(cidfile),
            "-v", f"{req.workdir}:/work",
            "-w", "/work",
            "--network", self._cfg.network,
        ]
        if self._cfg.user:
            argv += ["--user", self._cfg.user]
        if self._cfg.memory:
            argv += ["--memory", self._cfg.memory]
        if self._cfg.cpus:
            argv += ["--cpus", self._cfg.cpus]
        if secret_keys:
            argv += ["--env-file", str(env_file)]
        # non-secret env: explicit req.env + trace env, inlined (values not secret)
        for key, value in {**req.env, **dict(trace_env)}.items():
            argv += ["-e", f"{key}={value}"]
        for key, value in labels.items():
            argv += ["--label", f"{key}={value}"]
        argv.append(self._cfg.image)
        argv += list(req.argv)

        return PreparedRunPlan(
            argv=argv,
            env={},  # container env comes from -e / --env-file, not the docker CLI env
            container_name=name,
            labels=labels,
            env_file_keys=secret_keys,
            cidfile_path=cidfile,
            tmp_dir=tmp_dir,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_isolators.py -k docker -v`
Expected: PASS.

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/isolators.py tests/test_isolators.py
git commit -m "feat(execution): DockerIsolator.prepare (pure argv/mounts/labels/env)"
```

## Task 11: `DockerIsolator.materialize` (0700 dir, 0600 env-file, value validation)

**Files:**
- Modify: `maestro/execution/isolators.py` (add `materialize`, `transport_ref`, `wrap` to `DockerIsolator`)
- Test: `tests/test_isolators.py` (extend)

**Interfaces:**
- Produces: `DockerIsolator.materialize(plan) -> PreparedRun` writes the `0700` tmp dir and `0600` env-file from `os.environ` (reading secret values here, not in `prepare`), validating each value has no `\n`/`\r`/`NUL`; `transport_ref(prepared, pid) -> "docker:<name>"`; `wrap(...) -> DockerTaskHandle`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_isolators.py
import os as _os
import stat as _stat


def test_docker_materialize_writes_0600_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    iso = _docker_iso(secret_env=["ANTHROPIC_API_KEY"])
    plan = iso.prepare(
        _req(execution_id="e-mat"), trace_env={}, host_env=dict(_os.environ)
    )
    prepared = iso.materialize(plan)
    assert prepared.env_file is not None and prepared.env_file.exists()
    mode = _stat.S_IMODE(prepared.env_file.stat().st_mode)
    assert mode == 0o600
    dir_mode = _stat.S_IMODE(prepared.env_file.parent.stat().st_mode)
    assert dir_mode == 0o700
    assert "ANTHROPIC_API_KEY=sk-secret" in prepared.env_file.read_text()
    assert prepared.env_file in prepared.cleanup_paths
    assert plan.tmp_dir in prepared.cleanup_paths


def test_docker_materialize_rejects_newline_in_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("BAD", "line1\nline2")
    iso = _docker_iso(secret_env=["BAD"])
    plan = iso.prepare(_req(execution_id="e-bad"), trace_env={}, host_env=dict(_os.environ))
    with _pytest.raises(ValueError):
        iso.materialize(plan)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_isolators.py -k materialize -v`
Expected: FAIL (`materialize` still the protocol default / not implemented for docker).

- [ ] **Step 3: Implement `materialize`, `transport_ref`, `wrap`**

Add to `DockerIsolator` (import `os`, `stat`; import `DockerTaskHandle` lazily to avoid a cycle):

```python
    def materialize(self, plan: PreparedRunPlan) -> PreparedRun:
        assert plan.tmp_dir is not None
        plan.tmp_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(plan.tmp_dir, 0o700)
        env_file: Path | None = None
        cleanup: list[Path] = [plan.tmp_dir]
        if plan.env_file_keys:
            env_file = plan.tmp_dir / "env"
            lines = []
            for key in plan.env_file_keys:
                value = os.environ.get(key, "")
                if any(c in value for c in ("\n", "\r", "\x00")):
                    raise ValueError(f"secret {key} value has a forbidden control char")
                lines.append(f"{key}={value}")
            # 0600 from creation: open with O_CREAT|O_WRONLY and mode 0o600.
            fd = os.open(str(env_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            os.chmod(env_file, 0o600)  # defensive: umask could have narrowed only
            cleanup.append(env_file)
        return PreparedRun(plan=plan, env_file=env_file, cleanup_paths=cleanup)

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:
        return f"docker:{prepared.plan.container_name}"

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle:
        from maestro.execution.docker_handle import DockerTaskHandle

        return DockerTaskHandle(
            local=local,
            container_name=prepared.plan.container_name or "",
            expected_labels=prepared.plan.labels,
            cleanup_paths=prepared.cleanup_paths,
            docker=self._docker,
            ref=ref,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_isolators.py -k "materialize or docker" -v`
Expected: PASS (the `wrap` import of `DockerTaskHandle` resolves after Task 12; if running before Task 12, temporarily skip `wrap` — implement Task 12 next and re-run).

- [ ] **Step 5: Full checks + commit** (after Task 12 makes `wrap` importable)

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/isolators.py tests/test_isolators.py
git commit -m "feat(execution): DockerIsolator materialize (0600 env-file) + wrap"
```

## Task 12: `DockerTaskHandle` (honest lifecycle + ownership-checked cleanup)

**Files:**
- Create: `maestro/execution/docker_handle.py`
- Test: `tests/test_docker_handle.py` (create)

**Interfaces:**
- Consumes: `LocalTaskHandle`, `ExecutionResult`, `CollectResult`, `ExecutionHandleRef`, `DockerCli`.
- Produces: `class DockerTaskHandle` implementing `TaskHandle`: `poll`/`os_pid` delegate; `wait()` stops the container if the run timed out; `terminate`/`kill` targeted; `collect` no-op; `cleanup` ownership-checked `rm` + unlink.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docker_handle.py
from pathlib import Path

import pytest

from maestro.execution.docker_handle import DockerTaskHandle
from maestro.execution.models import ExecutionHandleRef, ExecutionResult
from datetime import UTC, datetime


class _FakeLocal:
    def __init__(self, result):
        self._result = result
        self.terminated = False
        self.killed = False

    @property
    def os_pid(self):
        return 111

    def poll(self):
        return self._result.exit_code

    async def wait(self):
        return self._result

    async def terminate(self, grace_seconds):
        self.terminated = True

    async def kill(self):
        self.killed = True


class _FakeDockerCli:
    def __init__(self, inspect_labels=None):
        self._labels = inspect_labels
        self.stopped = []
        self.killed = []
        self.removed = []

    async def inspect(self, name):
        if self._labels is None:
            return None
        return {"Config": {"Labels": self._labels}}

    async def stop(self, name, timeout):
        self.stopped.append(name)

    async def kill(self, name):
        self.killed.append(name)

    async def rm(self, name):
        self.removed.append(name)


def _handle(local, docker, cleanup_paths=None):
    ref = ExecutionHandleRef(
        backend_id="docker", run_id="t1",
        transport_ref="docker:maestro-e1", started_at=datetime.now(UTC),
    )
    return DockerTaskHandle(
        local=local, container_name="maestro-e1",
        expected_labels={"maestro.execution_id": "e1"},
        cleanup_paths=cleanup_paths or [], docker=docker, ref=ref,
    )


@pytest.mark.anyio
async def test_wait_timeout_stops_container():
    local = _FakeLocal(ExecutionResult(exit_code=None, output_log_path=Path("/l"), timed_out=True))
    docker = _FakeDockerCli()
    h = _handle(local, docker)
    result = await h.wait()
    assert result.timed_out is True
    assert docker.stopped == ["maestro-e1"]     # container stopped on timeout


@pytest.mark.anyio
async def test_collect_is_noop():
    h = _handle(_FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))), _FakeDockerCli())
    res = await h.collect()
    assert res.applied is False


@pytest.mark.anyio
async def test_cleanup_removes_when_label_matches(tmp_path):
    f = tmp_path / "env"
    f.write_text("X=1")
    docker = _FakeDockerCli(inspect_labels={"maestro.execution_id": "e1"})
    h = _handle(_FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))), docker, [f])
    await h.cleanup()
    assert docker.removed == ["maestro-e1"]
    assert not f.exists()                         # local secret file unlinked


@pytest.mark.anyio
async def test_cleanup_raises_on_label_mismatch(tmp_path):
    docker = _FakeDockerCli(inspect_labels={"maestro.execution_id": "OTHER"})
    h = _handle(_FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))), docker)
    with pytest.raises(RuntimeError):
        await h.cleanup()
    assert docker.removed == []                    # never removes a foreign container


@pytest.mark.anyio
async def test_cleanup_absent_container_still_unlinks(tmp_path):
    f = tmp_path / "env"
    f.write_text("X=1")
    docker = _FakeDockerCli(inspect_labels=None)   # inspect → None (absent)
    h = _handle(_FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))), docker, [f])
    await h.cleanup()
    assert docker.removed == []
    assert not f.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docker_handle.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.docker_handle`.

- [ ] **Step 3: Implement `DockerTaskHandle`**

```python
# maestro/execution/docker_handle.py
"""TaskHandle for a container run: composes a LocalTaskHandle (the attached
`docker run` process) with targeted container stop/kill/rm.
"""

import contextlib
import shutil

from maestro.execution.local import LocalTaskHandle
from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)


class DockerTaskHandle:
    """Wraps a LocalTaskHandle; owns its container's lifecycle."""

    def __init__(
        self,
        *,
        local: LocalTaskHandle,
        container_name: str,
        expected_labels: dict[str, str],
        cleanup_paths: list,
        docker,
        ref: ExecutionHandleRef,
    ) -> None:
        self._local = local
        self._name = container_name
        self._expected = expected_labels
        self._cleanup_paths = cleanup_paths
        self._docker = docker
        self.ref = ref

    @property
    def os_pid(self):
        return self._local.os_pid

    def poll(self):
        return self._local.poll()

    async def wait(self) -> ExecutionResult:
        result = await self._local.wait()
        if result.timed_out:
            # docker run was killed; ensure the container itself is stopped.
            await self._stop_container(grace=10.0)
        return result

    async def terminate(self, grace_seconds: float) -> None:
        await self._local.terminate(grace_seconds)
        await self._stop_container(grace=grace_seconds)

    async def kill(self) -> None:
        await self._local.kill()
        with contextlib.suppress(Exception):
            await self._docker.kill(self._name)

    async def collect(self) -> CollectResult:
        return CollectResult(applied=False, detail="docker: bind-mounted /work")

    async def cleanup(self) -> None:
        info = await self._docker.inspect(self._name)
        if info is not None:
            labels = (info.get("Config") or {}).get("Labels") or {}
            expected_id = self._expected.get("maestro.execution_id")
            if labels.get("maestro.execution_id") != expected_id:
                raise RuntimeError(
                    f"refusing to rm {self._name}: label mismatch "
                    f"(expected {expected_id}, got {labels.get('maestro.execution_id')})"
                )
            await self._docker.rm(self._name)
        # Always unlink local secret/cid/tmp artifacts, even if the container is gone.
        for path in self._cleanup_paths:
            with contextlib.suppress(OSError):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)

    async def _stop_container(self, grace: float) -> None:
        with contextlib.suppress(Exception):
            await self._docker.stop(self._name, grace)
        with contextlib.suppress(Exception):
            await self._docker.kill(self._name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_docker_handle.py tests/test_isolators.py -v`
Expected: PASS (Task 11's `wrap` now imports cleanly).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/docker_handle.py tests/test_docker_handle.py maestro/execution/isolators.py
git commit -m "feat(execution): DockerTaskHandle — honest lifecycle, ownership-checked cleanup"
```

## Task 13: Docker backend in the resolver (healthcheck + tool probe)

**Files:**
- Modify: `maestro/execution/resolver.py` (build a real docker backend)
- Modify: `maestro/execution/local.py` (`LocalBackend.healthcheck`/`can_run` become isolation-aware) OR add a small `DockerBackend` wrapper — see below.
- Test: `tests/test_backend_resolver.py` (extend, injected `DockerCli`)

**Interfaces:**
- Produces: `resolve("docker")` returns a `LocalBackend(DockerIsolator(cfg, docker))` whose `healthcheck()` verifies the daemon is reachable, `DOCKER_HOST` is not `ssh://`/`tcp://`, and the image exists; `can_run()` probes `required_tools` inside the image with a `--rm` helper container.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_backend_resolver.py
from maestro.execution.docker_cli import DockerCli
from maestro.execution.exec_config import DockerConfig, ExecutionConfig


def test_docker_resolves_when_config_present():
    r = BackendResolver(
        ExecutionConfig(docker=DockerConfig(image="maestro-runner:x"))
    )
    backend = r.resolve("docker")
    assert backend.id == "docker"


@pytest.mark.anyio
async def test_docker_healthcheck_rejects_ssh_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://gpu-box")
    r = BackendResolver(ExecutionConfig(docker=DockerConfig(image="i")))
    backend = r.resolve("docker")
    health = await backend.healthcheck()
    assert health.reachable is False
    assert "DOCKER_HOST" in health.detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backend_resolver.py -k docker -v`
Expected: FAIL (docker still raises `ExecutionConfigError`).

- [ ] **Step 3: Implement**

Make `LocalBackend` isolation-aware for `id`, `healthcheck`, and `can_run`. Add to `LocalBackend`:

```python
    @property
    def id(self) -> str:  # type: ignore[override]
        return self._isolator.id  # "bare" → "local"? see note

    # NOTE: keep the class attribute id = "local" for BareIsolator; override to
    # "docker" only when the isolator is a DockerIsolator. Simpler: pass an
    # explicit `backend_id` to LocalBackend.__init__ (default "local", "docker"
    # for the docker composition) and return it here.
```

Simplest concrete approach — give `LocalBackend.__init__` an explicit id:

```python
    def __init__(self, isolator=None, *, backend_id: str = "local", docker=None) -> None:
        if isolator is None:
            from maestro.execution.isolators import BareIsolator
            isolator = BareIsolator()
        self._isolator = isolator
        self._backend_id = backend_id
        self._docker = docker  # DockerCli | None, for healthcheck/can_run

    @property
    def id(self) -> str:
        return self._backend_id
```

Add docker-aware `healthcheck`/`can_run` (replace the existing bodies):

```python
    async def healthcheck(self) -> BackendHealth:
        if self._docker is None:
            return BackendHealth(reachable=True)
        host = os.environ.get("DOCKER_HOST", "")
        if host.startswith("ssh://") or host.startswith("tcp://"):
            return BackendHealth(
                reachable=False,
                detail=f"DOCKER_HOST={host!r} is remote; Phase 1 is local only",
            )
        if not await self._docker.version_ok():
            return BackendHealth(reachable=False, detail="docker daemon unreachable")
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        if self._docker is None:
            missing = [t for t in req.required_tools if shutil.which(t) is None]
            return CapabilityResult(ok=not missing, missing_tools=missing)
        # Docker: image presence is the Phase-1 capability gate; a full in-image
        # tool probe (a --rm helper container) is exercised by integration tests.
        image = getattr(self._isolator, "_cfg").image
        if not await self._docker.image_exists(image):
            return CapabilityResult(ok=False, missing_tools=[f"image:{image}"])
        return CapabilityResult(ok=True)
```

In `resolver.py`, build the docker backend:

```python
    def _build(self, name: str) -> ExecutionBackend:
        if name == "local":
            return LocalBackend()
        if name == "docker":
            if self._execution.docker is None:
                raise ExecutionConfigError(
                    "backend 'docker' selected but no execution.docker config"
                )
            from maestro.execution.docker_cli import DockerCli
            from maestro.execution.isolators import DockerIsolator

            docker = DockerCli()
            isolator = DockerIsolator(self._execution.docker, docker=docker)
            return LocalBackend(isolator, backend_id="docker", docker=docker)
        raise ExecutionConfigError(f"unknown backend '{name}'")
```

(The resolver test injects a fake `DockerCli` by monkeypatching `DockerCli` in `resolver`, or split `_build` to accept an injected cli for tests.)

- [ ] **Step 4: Run tests + suite**

Run: `uv run pytest tests/test_backend_resolver.py -v && uv run pytest`
Expected: PASS.

- [ ] **Step 5: Full checks + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/resolver.py maestro/execution/local.py tests/test_backend_resolver.py
git commit -m "feat(execution): docker backend in resolver (healthcheck + image gate)"
```

**Increment 1b done:** `backend: docker` now builds a runnable container backend; execution containers are labeled, secret-isolated, and stop/rm-clean. Persistence and recovery come next.

---

# Increment 1c — Durable identity (`execution_handles`) + recovery

## Task 14: `execution_handles` table + `backend` columns migration

**Files:**
- Modify: `maestro/database.py` (`SCHEMA_SQL` block near `:145`; `_apply_migrations` `ordered` list at `:395`; add migration methods)
- Test: `tests/test_execution_handles.py` (create)

**Interfaces:**
- Produces: table `execution_handles` (PK `execution_id`, CHECKed `entity_kind`/`state`, indexes `(state, backend_id)` and `(entity_kind, entity_id, attempt)`); nullable `backend` columns on `tasks` and `workstreams`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_execution_handles.py
import pytest

from maestro.database import Database


@pytest.mark.anyio
async def test_execution_handles_table_exists(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.initialize()
    cur = await db._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_handles'"
    )
    assert await cur.fetchone() is not None
    cur = await db._connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "backend" in cols
    cur = await db._connection.execute("PRAGMA table_info(workstreams)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "backend" in cols
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution_handles.py -v`
Expected: FAIL (no `execution_handles` table).

- [ ] **Step 3: Add the schema + migrations**

Add to the `SCHEMA_SQL` string (so fresh DBs get it) near the other `CREATE TABLE`s:

```sql
CREATE TABLE IF NOT EXISTS execution_handles (
    execution_id   TEXT PRIMARY KEY,
    entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
    entity_id      TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    backend_id     TEXT NOT NULL,
    transport_ref  TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','cleaned')),
    created_at     TEXT NOT NULL,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_exec_state_backend ON execution_handles (state, backend_id);
CREATE INDEX IF NOT EXISTS ix_exec_entity ON execution_handles (entity_kind, entity_id, attempt);
```

Add two migration methods (mirror `_migrate_tasks_arbiter_columns` at `:426` for the ALTER pattern and `_migrate_gate_approvals` at `:593` for the CREATE pattern):

```python
    async def _migrate_execution_handles(self) -> None:
        """Add the execution_handles table (idempotent)."""
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_handles (
                execution_id   TEXT PRIMARY KEY,
                entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
                entity_id      TEXT NOT NULL,
                attempt        INTEGER NOT NULL,
                backend_id     TEXT NOT NULL,
                transport_ref  TEXT NOT NULL,
                state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','cleaned')),
                created_at     TEXT NOT NULL,
                finished_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_exec_state_backend ON execution_handles (state, backend_id);
            CREATE INDEX IF NOT EXISTS ix_exec_entity ON execution_handles (entity_kind, entity_id, attempt);
            """
        )
        await self._connection.commit()

    async def _migrate_entity_backend_columns(self) -> None:
        """Add nullable `backend` to tasks and workstreams (idempotent)."""
        for table in ("tasks", "workstreams"):
            cur = await self._connection.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
            if "backend" not in cols:
                await self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN backend TEXT"
                )
        await self._connection.commit()
```

Append both to the `ordered` list in `_apply_migrations` with the next two version numbers (read the current max in that list and continue):

```python
            (N, "execution_handles", self._migrate_execution_handles),
            (N + 1, "entity_backend_columns", self._migrate_entity_backend_columns),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execution_handles.py -v && uv run pytest -k database -v`
Expected: PASS; existing DB tests green.

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/database.py tests/test_execution_handles.py
git commit -m "feat(db): execution_handles table + entity backend columns (migration)"
```

## Task 15: `start_execution` (atomic) + `mark_execution_state` + recovery query

**Files:**
- Modify: `maestro/database.py`
- Test: `tests/test_execution_handles.py` (extend)

**Interfaces:**
- Produces:
  - `async def start_execution(self, *, entity_kind, entity_id, expected_status, running_status, execution_id, backend_id, transport_ref, attempt) -> None` — one transaction: CAS the entity `expected_status → running_status`; insert `execution_handles(state="prepared")`. Raises `ConcurrentModificationError` if the CAS matches no row.
  - `async def mark_execution_state(self, execution_id, new_state, *, allowed_from: list[str]) -> None` — monotonic guarded update.
  - `async def get_open_execution_handles(self) -> list[dict]` — rows with `state IN ('prepared','running','terminal')` and `backend_id != 'local'` (recovery input).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_execution_handles.py
from datetime import UTC, datetime

from maestro.database import ConcurrentModificationError
from maestro.models import Task, TaskStatus


async def _seed_task(db, task_id="t1", status=TaskStatus.READY):
    task = Task(id=task_id, prompt="p", status=status)
    await db.create_task(task)
    return task


@pytest.mark.anyio
async def test_start_execution_atomic_cas_and_insert(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.initialize()
    await _seed_task(db)
    await db.start_execution(
        entity_kind="task", entity_id="t1",
        expected_status="ready", running_status="running",
        execution_id="e1", backend_id="docker",
        transport_ref="docker:maestro-e1", attempt=1,
    )
    got = await db.get_task("t1")
    assert got.status is TaskStatus.RUNNING
    rows = await db.get_open_execution_handles()
    assert any(r["execution_id"] == "e1" and r["state"] == "prepared" for r in rows)
    await db.close()


@pytest.mark.anyio
async def test_start_execution_cas_mismatch_raises_and_writes_nothing(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.initialize()
    await _seed_task(db, status=TaskStatus.DONE)   # not READY
    with pytest.raises(ConcurrentModificationError):
        await db.start_execution(
            entity_kind="task", entity_id="t1",
            expected_status="ready", running_status="running",
            execution_id="e1", backend_id="docker",
            transport_ref="docker:maestro-e1", attempt=1,
        )
    assert await db.get_open_execution_handles() == []   # no orphan row
    await db.close()


@pytest.mark.anyio
async def test_mark_execution_state_is_monotonic(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.initialize()
    await _seed_task(db)
    await db.start_execution(
        entity_kind="task", entity_id="t1", expected_status="ready",
        running_status="running", execution_id="e1", backend_id="docker",
        transport_ref="docker:maestro-e1", attempt=1,
    )
    await db.mark_execution_state("e1", "terminal", allowed_from=["prepared", "running"])
    # cleaned -> running must be impossible
    await db.mark_execution_state("e1", "cleaned", allowed_from=["terminal"])
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])  # no-op
    rows = {r["execution_id"]: r for r in await db.get_open_execution_handles()}
    assert "e1" not in rows        # 'cleaned' is filtered out of the open set
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution_handles.py -k "start_execution or monotonic" -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'start_execution'`.

- [ ] **Step 3: Implement the three methods**

```python
    async def start_execution(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        expected_status: str,
        running_status: str,
        execution_id: str,
        backend_id: str,
        transport_ref: str,
        attempt: int,
    ) -> None:
        """Atomically CAS the entity to RUNNING and insert a prepared handle."""
        table = "tasks" if entity_kind == "task" else "workstreams"
        cur = await self._connection.execute(
            f"UPDATE {table} SET status = ? WHERE id = ? AND status = ?",
            (running_status, entity_id, expected_status),
        )
        if cur.rowcount == 0:
            await self._connection.rollback()
            raise ConcurrentModificationError(
                f"{entity_kind} {entity_id}: status != {expected_status!r}"
            )
        await self._connection.execute(
            """
            INSERT INTO execution_handles
              (execution_id, entity_kind, entity_id, attempt, backend_id,
               transport_ref, state, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, NULL)
            """,
            (
                execution_id, entity_kind, entity_id, attempt, backend_id,
                transport_ref, datetime.now(UTC).isoformat(),
            ),
        )
        await self._connection.commit()

    async def mark_execution_state(
        self, execution_id: str, new_state: str, *, allowed_from: list[str]
    ) -> None:
        """Monotonic state update: only applies from an allowed prior state."""
        placeholders = ",".join("?" for _ in allowed_from)
        finished = (
            datetime.now(UTC).isoformat() if new_state in ("terminal", "cleaned") else None
        )
        await self._connection.execute(
            f"""
            UPDATE execution_handles
               SET state = ?, finished_at = COALESCE(?, finished_at)
             WHERE execution_id = ? AND state IN ({placeholders})
            """,
            (new_state, finished, execution_id, *allowed_from),
        )
        await self._connection.commit()

    async def get_open_execution_handles(self) -> list[dict]:
        """Rows a recovery pass must reconcile (non-cleaned, non-local)."""
        cur = await self._connection.execute(
            """
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at
              FROM execution_handles
             WHERE state IN ('prepared','running','terminal') AND backend_id != 'local'
            """
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in await cur.fetchall()]
```

Ensure `from datetime import UTC, datetime` is imported in `database.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execution_handles.py -v`
Expected: PASS.

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/database.py tests/test_execution_handles.py
git commit -m "feat(db): atomic start_execution + monotonic state + recovery query"
```

## Task 16: Scheduler — mint identity, persist, finalize→state (Mode 1)

**Files:**
- Modify: `maestro/scheduler.py` (`_spawn_task` dispatch; `_monitor_running_tasks` finalize branch from Task 5)
- Test: `tests/test_scheduler_docker_wiring.py` (create — fake backend, no real docker)

**Interfaces:**
- Consumes: `start_execution`, `mark_execution_state`, `_dispatch_committed_transition(task, frm=)` (`scheduler.py:333`).
- Produces: docker-backed dispatch mints `execution_id`, populates request launch fields, calls `start_execution` + committed transition (in place of the plain `_transition` to RUNNING); finalize marks `terminal`, then `cleaned` iff `fin.cleaned`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_docker_wiring.py
import uuid

import pytest

# This test drives _spawn_task with a fake backend whose run() returns a fake
# handle, asserting an execution_handles row is created for a docker task and
# marked cleaned after finalize. Construct the Scheduler with an in-memory DB
# and a project config carrying execution.docker + one task backend: docker.
# (Follow the construction pattern in tests/test_scheduler_arbiter_integration.py.)


@pytest.mark.anyio
async def test_docker_task_persists_and_cleans_execution_handle(scheduler_docker_env):
    sched, db, task_id = scheduler_docker_env
    await sched._spawn_task(await db.get_task(task_id))
    rows = await db.get_open_execution_handles()
    assert any(r["entity_id"] == task_id and r["backend_id"] == "docker" for r in rows)
    # drive one monitor tick to completion
    await sched._monitor_running_tasks()
    # after finalize with a successful fake cleanup, the row is 'cleaned'
    assert all(r["entity_id"] != task_id for r in await db.get_open_execution_handles())
```

Provide a `scheduler_docker_env` fixture in the test file that builds the Scheduler with a monkeypatched resolver returning a fake backend (fake `run()` → a fake handle whose `poll()` returns 0, `wait()` returns exit 0, `collect`/`cleanup` succeed). Mirror the existing scheduler-test construction helpers.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_docker_wiring.py -v`
Expected: FAIL (no execution_handles row created — wiring absent).

- [ ] **Step 3: Wire the dispatch and finalize**

In `_spawn_task`, after resolving the backend (Task 8) and before the RUNNING transition, branch on docker:

```python
        backend = self._backends.resolve(task.backend)
        if backend.id != "local":
            execution_id = str(uuid.uuid4())
            request = request.model_copy(update={
                "backend_id": backend.id,
                "execution_id": execution_id,
                "entity_kind": "task",
                "attempt": task.retry_count + 1,
            })
            await self._db.start_execution(
                entity_kind="task", entity_id=task.id,
                expected_status=TaskStatus.READY.value,
                running_status=TaskStatus.RUNNING.value,
                execution_id=execution_id, backend_id=backend.id,
                transport_ref=f"{backend.id}:maestro-{execution_id}",
                attempt=task.retry_count + 1,
            )
            running_task_record = await self._db.get_task(task.id)
            await self._dispatch_committed_transition(
                running_task_record, frm=TaskStatus.READY
            )
        else:
            # unchanged local path: the existing _transition(READY→RUNNING)
            request = request.model_copy(update={"backend_id": backend.id})
            await self._transition(
                task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
            )
        cap = await backend.can_run(request)
        # ... existing not-available handling ...
        handle = await backend.run(request)
```

(Import `uuid`. Keep the existing RUNNING-transition call for the local path exactly where it is today — the branch only diverts the docker case.)

Add `execution_id: str | None = None` to `RunningTask` (after `finalize_task`) and set it when constructing the `RunningTask` in `_spawn_task` (from the minted `execution_id`, or `None` for the local path). Then, in the finalize branch of `_monitor_running_tasks` (Task 5), record the execution-handle state after finalize by reading that field — no string parsing:

```python
            if return_code is not None:
                fin = await asyncio.shield(ensure_finalize_task(running_task))
                exec_id = running_task.execution_id
                if exec_id is not None:
                    await self._db.mark_execution_state(
                        exec_id, "terminal", allowed_from=["prepared", "running"]
                    )
                    if fin.cleaned:
                        await self._db.mark_execution_state(
                            exec_id, "cleaned", allowed_from=["terminal"]
                        )
                if fin.collect_error or fin.cleanup_error:
                    _obs_log.warning(
                        "execution.finalize.resource_fault",
                        task_id=task_id,
                        collect_error=fin.collect_error,
                        cleanup_error=fin.cleanup_error,
                    )
                await self._handle_task_completion(
                    task_id, running_task, fin.execution.exit_code
                )
                completed.append(task_id)
```

Apply the same `mark_execution_state("terminal", ...)` (no `cleaned`, since the container was force-stopped) in the timeout branch after its `ensure_finalize_task`.

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/test_scheduler_docker_wiring.py -v && uv run pytest`
Expected: PASS; local-path scheduler tests unchanged (the `backend.id == "local"` branch is today's code path).

- [ ] **Step 5: Full checks + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/scheduler.py tests/test_scheduler_docker_wiring.py
git commit -m "feat(scheduler): docker dispatch persists execution identity + state"
```

## Task 17: Orchestrator — same wiring (Mode 2)

**Files:**
- Modify: `maestro/orchestrator.py` (`RunningWorkstream`, spawn site `:729-739`, monitor)
- Test: `tests/test_orchestrator_docker_wiring.py` (create — fake backend)

**Interfaces:** identical to Task 16 with `entity_kind="workstream"`, `WorkstreamStatus.READY → RUNNING`, and the orchestrator's `_dispatch_committed_transition` equivalent (the orchestrator uses its own `TransitionDispatcher`, imported at `:51`).

- [ ] **Step 1: Write the failing test** — mirror Task 16 for a workstream: a docker workstream creates an execution_handles row with `entity_kind="workstream"` and it becomes `cleaned` after the monitor finalizes. Build the orchestrator with a fake backend (follow the construction in `tests/test_orchestrator*.py`).

- [ ] **Step 2: Run test to verify it fails.** Run: `uv run pytest tests/test_orchestrator_docker_wiring.py -v` → FAIL (no row).

- [ ] **Step 3: Wire spawn + finalize** — apply the Task-16 branch in the workstream spawn path: mint `execution_id`, populate the request launch fields (`entity_kind="workstream"`, `attempt = workstream.retry_count + 1`), call `start_execution(entity_kind="workstream", expected_status=WorkstreamStatus.READY.value, running_status=WorkstreamStatus.RUNNING.value, ...)`, re-read, dispatch the committed transition. Add `finalize_task`/`execution_id` to `RunningWorkstream` and the single-owner finalize in the orchestrator monitor (mirror Task 5 + Task 16's `mark_execution_state` calls). The local path is unchanged.

- [ ] **Step 4: Run test + suite.** Run: `uv run pytest tests/test_orchestrator_docker_wiring.py -v && uv run pytest` → PASS.

- [ ] **Step 5: Full checks + commit.**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/orchestrator.py tests/test_orchestrator_docker_wiring.py
git commit -m "feat(orchestrator): docker dispatch persists execution identity + state (Mode 2)"
```

## Task 18: Recovery — probe-by-label, fail-closed + GC sweep

**Files:**
- Create: `maestro/execution/docker_recovery.py`
- Modify: `maestro/recovery.py` (Mode 1), `maestro/orchestrator.py` recovery reconcile (Mode 2)
- Test: `tests/test_docker_recovery.py` (create)

**Interfaces:**
- Produces: `@dataclass RecoveryVerdict(needs_review: bool, reason: str)`; `async def probe_execution(execution_id: str, docker: DockerCli) -> RecoveryVerdict` — any found container (any state) / ambiguous / daemon-error → `needs_review=True`; none → `False`. A `terminal`-not-`cleaned` row triggers an ownership-checked GC (reuse `DockerTaskHandle.cleanup` semantics) with no status change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docker_recovery.py
import pytest

from maestro.execution.docker_recovery import probe_execution


class _Docker:
    def __init__(self, ids, labels=None, raise_ps=False):
        self._ids = ids
        self._labels = labels
        self._raise_ps = raise_ps

    async def ps_ids_by_label(self, key, value):
        if self._raise_ps:
            raise RuntimeError("daemon down")
        return self._ids

    async def inspect(self, name):
        return {"Config": {"Labels": self._labels or {}}}


@pytest.mark.anyio
async def test_no_container_proceeds():
    v = await probe_execution("e1", _Docker(ids=[]))
    assert v.needs_review is False


@pytest.mark.anyio
async def test_found_container_needs_review():
    v = await probe_execution("e1", _Docker(ids=["c1"], labels={"maestro.execution_id": "e1"}))
    assert v.needs_review is True


@pytest.mark.anyio
async def test_daemon_error_fails_closed():
    v = await probe_execution("e1", _Docker(ids=[], raise_ps=True))
    assert v.needs_review is True


@pytest.mark.anyio
async def test_ambiguous_multiple_needs_review():
    v = await probe_execution("e1", _Docker(ids=["c1", "c2"]))
    assert v.needs_review is True
```

- [ ] **Step 2: Run test to verify it fails.** Run: `uv run pytest tests/test_docker_recovery.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `probe_execution`**

```python
# maestro/execution/docker_recovery.py
"""Fail-closed recovery classification for docker-backed executions."""

from dataclasses import dataclass


@dataclass
class RecoveryVerdict:
    needs_review: bool
    reason: str


async def probe_execution(execution_id: str, docker) -> RecoveryVerdict:
    """Any confirmed container (any state), ambiguity, or probe error → review."""
    try:
        ids = await docker.ps_ids_by_label("maestro.execution_id", execution_id)
    except Exception as e:  # noqa: BLE001 — daemon/inspect fault fails closed
        return RecoveryVerdict(True, f"probe failed: {e}")
    if not ids:
        return RecoveryVerdict(False, "no container found")
    if len(ids) > 1:
        return RecoveryVerdict(True, f"ambiguous: {len(ids)} containers")
    info = await docker.inspect(ids[0])
    labels = (info or {}).get("Config", {}).get("Labels") or {}
    if labels.get("maestro.execution_id") != execution_id:
        return RecoveryVerdict(True, "label mismatch on found container")
    return RecoveryVerdict(True, "live/leftover container found")
```

- [ ] **Step 4: Wire into recovery paths**

In `maestro/recovery.py`, before the Mode-1 `RUNNING`/`VALIDATING → FAILED → READY` transition (`recovery.py:63`, `:127`), for a task whose resolved backend is non-local: look up its open `execution_handles` row (`get_open_execution_handles`), call `probe_execution(exec_id, docker)`; on `needs_review` route the task to `NEEDS_REVIEW` instead of `READY`. For a `terminal`-not-`cleaned` row, run the ownership-checked GC (construct a `DockerTaskHandle`-equivalent cleanup or a direct `DockerCli` inspect+rm guarded by the execution_id label) and `mark_execution_state(..., "cleaned", allowed_from=["terminal"])` — no status change. Do the equivalent in the orchestrator's startup reconcile (Mode 2) alongside its existing pid-liveness checks. Pass a `DockerCli` into recovery (default `DockerCli()`), injectable for tests.

Add a wiring test asserting a task with a live container is sent to `NEEDS_REVIEW`, not `READY` (fake `get_open_execution_handles` + fake `DockerCli`).

- [ ] **Step 5: Run tests + suite + commit**

Run: `uv run pytest tests/test_docker_recovery.py -v && uv run pytest`
Expected: PASS.

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/docker_recovery.py maestro/recovery.py maestro/orchestrator.py tests/test_docker_recovery.py
git commit -m "feat(recovery): probe-by-label fail-closed + terminal-row GC (both modes)"
```

**Increment 1c done:** every docker attempt has a durable, uniquely-identified handle; crashes classify fail-closed to `NEEDS_REVIEW`; leftover containers are GC'd.

---

# Increment 1d — Integration tests + docs

## Task 19: Opt-in Docker integration tests (auto-skip without docker)

**Files:**
- Create: `tests/test_docker_integration.py`
- Test: itself

**Interfaces:** Consumes the full stack (`BackendResolver` → `LocalBackend(DockerIsolator)` → `DockerTaskHandle`).

- [ ] **Step 1: Write the integration tests (skip when docker/image absent)**

```python
# tests/test_docker_integration.py
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

IMAGE = "python:3.12-slim"  # a small public image with python; no agent CLIs needed


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


skip_no_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker not available"
)


def _pull_image():
    subprocess.run(["docker", "pull", IMAGE], check=True, capture_output=True)


@skip_no_docker
async def test_bind_mount_collect_is_noop_file_visible_on_host(tmp_path: Path):
    _pull_image()
    from maestro.execution.exec_config import DockerConfig
    from maestro.execution.resolver import BackendResolver, ExecutionConfig
    from maestro.execution.models import CollectPolicy, ExecutionRequest

    wd = tmp_path / "wd"
    wd.mkdir()
    r = BackendResolver(ExecutionConfig(docker=DockerConfig(image=IMAGE, network="none")))
    backend = r.resolve("docker")
    req = ExecutionRequest(
        run_id="t1", execution_id="itest-1", entity_kind="task", attempt=1,
        backend_id="docker",
        argv=["python", "-c", "open('/work/out.txt','w').write('from-container')"],
        workdir=wd, log_path=wd / "log", collect=CollectPolicy(mode="none"),
    )
    handle = await backend.run(req)
    from maestro.execution.finalize import finalize_handle
    fin = await finalize_handle(handle)
    assert fin.execution.exit_code == 0
    # collect is a no-op, yet the file exists on the host (bind mount)
    assert (wd / "out.txt").read_text() == "from-container"
    assert fin.cleaned is True


@skip_no_docker
async def test_success_leaves_no_container(tmp_path: Path):
    _pull_image()
    # ... run a trivial container as above, finalize, then assert
    # `docker ps -a --filter label=maestro.execution_id=itest-2` returns empty.
    ...  # implement mirroring the test above; assert no leftover container


@skip_no_docker
async def test_timeout_kills_and_removes_container(tmp_path: Path):
    _pull_image()
    # argv sleeps longer than timeout_seconds; assert result.timed_out and that
    # `docker ps -a --filter label=maestro.execution_id=itest-3` is empty after cleanup.
    ...


@skip_no_docker
async def test_opt_in_network_via_local_docker_network(tmp_path: Path):
    # Create a local docker network, run with network=<that>, assert the
    # container can reach another container on it — NOT the public internet.
    # `docker network create maestro-itest-net` ... cleanup in finally.
    ...
```

Flesh out the three stubbed tests following the first (they are structurally identical: build a request, `backend.run`, `finalize_handle`, assert). Keep every network assertion against a **local** docker network — never the public internet.

- [ ] **Step 2: Run (locally, with docker)**

Run: `uv run pytest tests/test_docker_integration.py -v`
Expected: PASS locally; SKIPPED in CI without docker.

- [ ] **Step 3: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add tests/test_docker_integration.py
git commit -m "test(execution): opt-in docker integration tests (auto-skip)"
```

## Task 20: Docs, example config, observability spans

**Files:**
- Modify: `maestro/CLAUDE.md:155` (state-polling drift note, §15)
- Create: `examples/with-docker.yaml`
- Modify: `maestro/execution/local.py` / dispatch sites (obs spans `execution.dispatch`, `execution.run`, §14)
- Modify: `tests/test_examples_smoke.py` (the new example is picked up by the parametrized smoke test)

- [ ] **Step 1: Write the example config**

```yaml
# examples/with-docker.yaml
# Local Docker isolation (Phase 1). No `execution` block → local+bare, as before.
execution:
  default_backend: local
  docker:
    image: maestro-runner:2026-07-23   # must have the agent CLIs pre-installed
    network: none                      # secure default; widen explicitly if needed
    memory: 8g
    cpus: "2"
    user: "1000:1000"
    secret_env: [ANTHROPIC_API_KEY]    # NAMES only; values read from the host env

tasks:
  - id: sandbox-refactor
    prompt: "Refactor the parser; keep the public API stable."
    agent_type: claude_code
    backend: docker                    # this task runs in a container
  - id: local-notes
    prompt: "Summarize the changes."
    agent_type: claude_code
    # no backend: → default_backend (local)
```

- [ ] **Step 2: Verify the example smoke test picks it up**

Run: `uv run pytest tests/test_examples_smoke.py -v`
Expected: PASS (the parametrized `examples/*.yaml` loader validates the new file; provide a dummy `ANTHROPIC_API_KEY` env if the loader substitutes it, following the existing smoke-test env setup).

- [ ] **Step 3: Update `CLAUDE.md` drift note**

Replace the `maestro/CLAUDE.md:155` "state polling deprecated" line with a note that Phase 1 adds a container-backed execution path (and later phases reintroduce polling for remote executors); keep it one or two sentences.

- [ ] **Step 4: Add observability spans**

Wrap the dispatch in `execution.dispatch` (attributes: `backend_id`, `isolation`) and the `backend.run` call in `execution.run`, using the vendored `obs.span(...)` already used in the scheduler (`obs.span("task.spawn")`). Record `backend_id`/`isolation` in the completion event. Add a test asserting the span/attributes if the codebase has an obs-capture harness (see `tests/test_scheduler_observability.py`); otherwise assert the completion event carries `backend_id`.

- [ ] **Step 5: Full checks + commit**

```bash
uv run pytest && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/CLAUDE.md examples/with-docker.yaml maestro/execution/local.py maestro/scheduler.py tests/test_examples_smoke.py
git commit -m "docs(execution): docker example + CLAUDE.md drift note + obs spans"
```

**Increment 1d done — Phase 1 complete.** Open a PR from `design/docker-isolation-phase1`; address the Copilot review per the repo git-workflow; a human merges.

---

## Acceptance checklist (verify before opening the PR)

- [ ] No `execution` config → full suite green; local runtime path unchanged (only the schema migration runs).
- [ ] `backend: docker` runs a Mode-1 task and a Mode-2 workstream in a local container with the workspace bind-mounted; results land on the host.
- [ ] Every terminal path (success / failure / timeout / cancellation / shutdown) finalizes exactly once; no container remains after a successful run.
- [ ] A simulated crash with a live container classifies `NEEDS_REVIEW`, never a silent re-run.
- [ ] Secret values never appear in argv, logs, event log, or DB (grep the log + `execution_handles` rows in a docker run).
- [ ] The four must-have lifecycle tests + the fail-fast config tests pass.
- [ ] `uv run pytest`, `uv run pyrefly check`, `uv run ruff format .`, `uv run ruff check .` all clean.

## Deferred (not in this plan — spec "Deferred / follow-ups")

- Validation through the execution layer (`validation_backend: same|local|<backend>`).
- Full named-`backends:{}` registry (second isolator/transport).
- SSH / remote transports, remote `plan --full` (Phase 2).
- Publishing a `maestro-runner` image (Phase 1 consumes a user-provided image).
