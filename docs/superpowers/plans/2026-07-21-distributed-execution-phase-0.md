# Distributed Execution — Phase 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a transport-agnostic execution layer (`ExecutionRequest` /
`TaskHandle` / `ExecutionBackend` + `LocalBackend`) and migrate both the Mode 1
scheduler and the Mode 2 orchestrator onto it, with **zero behavior change**.

**Architecture:** A new `maestro/execution/` package defines the contract
(pydantic models + two `Protocol`s) and a single `LocalBackend` whose
`LocalTaskHandle` wraps an `asyncio.subprocess.Process`. Spawners stop calling
`subprocess.Popen` directly and instead `build_request(...) -> ExecutionRequest`
("what to run"); the backend owns the spawn ("where/how to run"). Both the
scheduler (currently sync `Popen`) and the orchestrator (already async
`asyncio.subprocess.Process`) converge on the async handle. Validator rewiring
is explicitly deferred to Phase 2a (see Non-Goals).

**Tech Stack:** Python 3.12, pydantic v2, asyncio, pytest (+ pytest-asyncio,
`asyncio_mode = "auto"`), uv, ruff, pyrefly.

## Global Constraints

- **Package management: `uv` only.** Run tests with `uv run pytest`; never `pip`.
- **Zero behavior change.** With no `execution` config, every path must behave
  byte-identically to today (`local + bare`). This is the acceptance bar for
  every task.
- **Type hints required on all code**; run `pyrefly check` after each task and
  fix errors.
- **Ruff:** `line-length = 88`, `target py312`, double quotes,
  `lines-after-imports = 2`. Run `uv run ruff format . && uv run ruff check .`.
- **Pytest:** `--strict-markers` is on — do **not** introduce new markers in
  Phase 0. `asyncio_mode = "auto"` (async tests need no decorator). Coverage
  gate `fail_under = 80`.
- **Spawner entry-point keys are fixed:** `claude_code`, `codex_cli`, `aider`,
  `announce`, `opencode` (note `codex_cli`, not `codex`) —
  `pyproject.toml:44-49`. Do not rename.
- **Additive-first, remove-dead-last.** Keep `spawn()`/`is_available()` working
  until all consumers are migrated; delete them only in the final task so every
  intermediate task keeps the suite green.

---

## File Structure

**New package `maestro/execution/`:**
- `maestro/execution/__init__.py` — re-exports the public contract.
- `maestro/execution/models.py` — `ExecutionRequest`, `CollectPolicy`,
  `ProgressMirrorPolicy`, `ExecutionResult`, `ExecutionHandleRef`,
  `CollectResult`, `BackendHealth`, `CapabilityResult`, `ProbeResult`.
- `maestro/execution/backend.py` — `TaskHandle` + `ExecutionBackend` Protocols.
- `maestro/execution/local.py` — `LocalBackend`, `LocalTaskHandle`.
- `maestro/execution/env.py` — `build_local_env()` (consolidates the three
  copies of `{**os.environ, **child_env()}`).

**Modified:**
- `maestro/spawners/base.py` — add `build_request()` + `can_build_request()` to
  the ABC; keep `spawn()`/`is_available()` until Task 7.
- `maestro/spawners/{claude_code,codex,aider,opencode,announce}.py` — add
  `build_request()` + `can_build_request()`.
- `maestro/scheduler.py` — `RunningTask.process` → `handle`; spawn call site;
  poll/terminate/kill/wait sites; availability preflight.
- `maestro/orchestrator.py` — `RunningWorkstream.process` → `handle`;
  spec-runner spawn; returncode/terminate/kill/wait/pid sites.

**New tests:**
- `tests/test_execution_models.py`, `tests/test_execution_local.py`.
**Modified tests:** `tests/test_spawners.py`, `tests/test_scheduler.py`,
`tests/test_orchestrator.py` (fakes: `Popen` → handle).

---

## Task 1: Execution contract models

**Files:**
- Create: `maestro/execution/__init__.py`
- Create: `maestro/execution/models.py`
- Test: `tests/test_execution_models.py`

**Interfaces:**
- Produces: `ExecutionRequest`, `CollectPolicy`, `ProgressMirrorPolicy`,
  `ExecutionResult`, `ExecutionHandleRef`, `CollectResult`, `BackendHealth`,
  `CapabilityResult`, `ProbeResult` — all pydantic v2 `BaseModel`s.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_execution_models.py
from datetime import UTC, datetime
from pathlib import Path

from maestro.execution.models import (
    CollectPolicy,
    ExecutionRequest,
    ExecutionResult,
    ExecutionHandleRef,
)


def test_execution_request_minimal_defaults():
    req = ExecutionRequest(
        run_id="r1",
        argv=["echo", "hi"],
        workdir=Path("/tmp/wd"),
        log_path=Path("/tmp/wd/out.log"),
        collect=CollectPolicy(mode="none"),
    )
    assert req.env == {}
    assert req.secret_env == []
    assert req.inherit_env is False
    assert req.capture_output is False
    assert req.progress_mirror is None
    assert req.labels == {}
    assert req.required_tools == []
    # mutable defaults are per-instance, not shared
    req.env["A"] = "1"
    other = ExecutionRequest(
        run_id="r2", argv=["true"], workdir=Path("/tmp"),
        log_path=Path("/tmp/o.log"), collect=CollectPolicy(mode="none"),
    )
    assert other.env == {}


def test_collect_policy_defaults_and_modes():
    p = CollectPolicy(mode="scope_paths")
    assert p.exclude == [".git/**", ".maestro/**"]
    assert p.conflict_policy == "fail"
    assert p.on_failure == "collect"


def test_execution_result_capture_fields():
    r = ExecutionResult(exit_code=0, output_log_path=Path("/tmp/o.log"))
    assert r.stdout_tail == ""
    assert r.stderr_tail == ""
    assert r.timed_out is False
    assert r.error_message is None


def test_handle_ref_roundtrip():
    ref = ExecutionHandleRef(
        backend_id="local",
        run_id="r1",
        transport_ref="local_pid:123",
        started_at=datetime.now(UTC),
    )
    assert ref.status_marker is None
    assert ref.workdir_mirror_path is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.execution'`.

- [ ] **Step 3: Write the models**

```python
# maestro/execution/models.py
"""Transport-agnostic execution contract models.

These describe a run request and its lifecycle independently of *where* it
runs (local process, remote SSH host, container). Phase 0 uses only the
subset needed by LocalBackend; the remaining fields (progress_mirror,
secret_env, status_marker, mirror paths) are part of the frozen contract and
are consumed by later phases.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class CollectPolicy(BaseModel):
    """Terminal file-application policy (apply remote changes back locally)."""

    mode: Literal["none", "whole_worktree", "scope_paths", "patch"]
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=lambda: [".git/**", ".maestro/**"])
    conflict_policy: Literal["fail", "overwrite"] = "fail"
    on_failure: Literal["collect", "skip"] = "collect"


class ProgressMirrorPolicy(BaseModel):
    """Live, during-run mirror of executor state (orthogonal to collect)."""

    kind: Literal["spec_runner_sqlite"]
    remote_globs: list[str]
    local_dir: Path
    interval_seconds: float


class ExecutionRequest(BaseModel):
    """A transport-agnostic run request and its lifecycle description."""

    run_id: str
    argv: list[str]
    workdir: Path
    log_path: Path
    stdin: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)
    inherit_env: bool = False
    timeout_seconds: float | None = None
    capture_output: bool = False
    collect: CollectPolicy
    progress_mirror: ProgressMirrorPolicy | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    required_tools: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    """Terminal outcome of a run."""

    exit_code: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_log_path: Path
    timed_out: bool = False
    error_message: str | None = None


class ExecutionHandleRef(BaseModel):
    """Persisted identity of a run; survives a center restart."""

    backend_id: str
    run_id: str
    transport_ref: str
    status_marker: str | None = None
    started_at: datetime
    workdir_mirror_path: Path | None = None
    state_mirror_path: Path | None = None


class CollectResult(BaseModel):
    applied: bool
    files_changed: int = 0
    detail: str = ""


class BackendHealth(BaseModel):
    reachable: bool
    detail: str = ""


class CapabilityResult(BaseModel):
    ok: bool
    missing_tools: list[str] = Field(default_factory=list)


class ProbeResult(BaseModel):
    alive: bool
    exit_code: int | None = None
    detail: str = ""
```

```python
# maestro/execution/__init__.py
"""Maestro execution layer: transport-agnostic run contract + backends."""

from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectPolicy,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
    ProgressMirrorPolicy,
)


__all__ = [
    "BackendHealth",
    "CapabilityResult",
    "CollectPolicy",
    "CollectResult",
    "ExecutionHandleRef",
    "ExecutionRequest",
    "ExecutionResult",
    "ProbeResult",
    "ProgressMirrorPolicy",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execution_models.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff format . && uv run ruff check maestro/execution && uv run pyrefly check`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add maestro/execution/__init__.py maestro/execution/models.py tests/test_execution_models.py
git commit -m "feat(execution): add transport-agnostic contract models"
```

---

## Task 2: Backend + TaskHandle Protocols

**Files:**
- Create: `maestro/execution/backend.py`
- Test: `tests/test_execution_models.py` (append a structural test)

**Interfaces:**
- Consumes: `ExecutionRequest`, `ExecutionResult`, `ExecutionHandleRef`,
  `CollectResult`, `BackendHealth`, `CapabilityResult`, `ProbeResult` (Task 1).
- Produces: `TaskHandle` (Protocol), `ExecutionBackend` (Protocol).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_execution_models.py
def test_protocols_importable():
    from maestro.execution.backend import ExecutionBackend, TaskHandle

    # Protocols are runtime-checkable enough to reference; just assert identity.
    assert TaskHandle.__name__ == "TaskHandle"
    assert ExecutionBackend.__name__ == "ExecutionBackend"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution_models.py::test_protocols_importable -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.execution.backend'`.

- [ ] **Step 3: Write the Protocols**

```python
# maestro/execution/backend.py
"""Execution backend and task-handle protocols.

`TaskHandle` replaces BOTH the scheduler's synchronous `subprocess.Popen`
and the orchestrator's `asyncio.subprocess.Process`. `poll()` is SYNC and
cached-only — it must never do network I/O (a remote backend updates the
cache from a local monitor task).
"""

from typing import Protocol, runtime_checkable

from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)


@runtime_checkable
class TaskHandle(Protocol):
    """Handle to a running (or finished) execution."""

    ref: ExecutionHandleRef

    @property
    def os_pid(self) -> int | None:
        """Local OS pid if the run is a local process, else None.

        Used by orchestrator recovery, which persists a pid and probes it
        with os.kill(pid, 0). Remote handles return None.
        """
        ...

    def poll(self) -> int | None:
        """Non-blocking, cached exit code (None while running). No I/O."""
        ...

    async def wait(self) -> ExecutionResult:
        """Await terminal completion and return the result."""
        ...

    async def terminate(self, grace_seconds: float) -> None:
        """Ask the process to stop; escalate after grace_seconds if needed."""
        ...

    async def kill(self) -> None:
        """Force-kill and reap."""
        ...

    async def collect(self) -> CollectResult:
        """Apply remote file changes back locally (no-op for LocalBackend)."""
        ...

    async def cleanup(self) -> None:
        """Release backend resources (remote tmp/container/env-file)."""
        ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """Runs an ExecutionRequest and yields a TaskHandle."""

    id: str

    async def healthcheck(self) -> BackendHealth:
        """Is the transport reachable (fail-fast before dispatch)?"""
        ...

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        """Are req.required_tools present on the target executor?"""
        ...

    async def run(self, req: ExecutionRequest) -> TaskHandle:
        """Start the run."""
        ...

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        """Is a persisted run still alive (post-restart recovery)?"""
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execution_models.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff format . && uv run ruff check maestro/execution && uv run pyrefly check`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add maestro/execution/backend.py tests/test_execution_models.py
git commit -m "feat(execution): add TaskHandle and ExecutionBackend protocols"
```

---

## Task 3: LocalBackend + LocalTaskHandle

**Files:**
- Create: `maestro/execution/env.py`
- Create: `maestro/execution/local.py`
- Test: `tests/test_execution_local.py`

**Interfaces:**
- Consumes: `ExecutionRequest`, `ExecutionResult`, `ExecutionHandleRef`,
  `CollectResult`, `BackendHealth`, `CapabilityResult`, `ProbeResult`,
  `TaskHandle`, `ExecutionBackend`.
- Produces:
  - `build_local_env(req: ExecutionRequest) -> dict[str, str]`
  - `class LocalTaskHandle` with `ref`, `os_pid`, `poll()`, `wait()`,
    `terminate()`, `kill()`, `collect()`, `cleanup()`.
  - `class LocalBackend` with `id = "local"`, `healthcheck()`, `can_run()`,
    `run(req) -> LocalTaskHandle`, `probe(ref)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_execution_local.py
import os
from pathlib import Path

from maestro.execution.local import LocalBackend, build_local_env
from maestro.execution.models import CollectPolicy, ExecutionRequest


def _req(tmp_path: Path, argv: list[str], **kw) -> ExecutionRequest:
    return ExecutionRequest(
        run_id="r1",
        argv=argv,
        workdir=tmp_path,
        log_path=tmp_path / "out.log",
        collect=CollectPolicy(mode="none"),
        **kw,
    )


async def test_run_streams_to_log_and_reports_exit_code(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sh", "-c", "echo hello; exit 0"]))
    result = await handle.wait()
    assert result.exit_code == 0
    assert result.timed_out is False
    assert (tmp_path / "out.log").read_text().strip() == "hello"
    assert handle.poll() == 0
    await handle.cleanup()


async def test_run_nonzero_exit(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sh", "-c", "exit 3"]))
    result = await handle.wait()
    assert result.exit_code == 3


async def test_capture_output_populates_tails(tmp_path):
    backend = LocalBackend()
    req = _req(tmp_path, ["sh", "-c", "echo out; echo err 1>&2"], capture_output=True)
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    assert "out" in result.stdout_tail
    assert "err" in result.stderr_tail


async def test_timeout_kills_and_flags(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sleep", "5"], timeout_seconds=1))
    result = await handle.wait()
    assert result.timed_out is True
    assert result.exit_code is None


async def test_poll_is_none_while_running(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sleep", "2"]))
    assert handle.poll() is None
    assert handle.os_pid is not None and handle.os_pid > 0
    await handle.kill()
    await handle.wait()


async def test_can_run_missing_tool(tmp_path):
    backend = LocalBackend()
    req = _req(tmp_path, ["true"], required_tools=["definitely-not-a-real-binary"])
    cap = await backend.can_run(req)
    assert cap.ok is False
    assert "definitely-not-a-real-binary" in cap.missing_tools


def test_build_local_env_inherit(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_MARKER", "xyz")
    req = _req(tmp_path, ["true"], inherit_env=True)
    env = build_local_env(req)
    assert env["MY_MARKER"] == "xyz"  # full inheritance == today's spawn_env()


def test_build_local_env_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_ONE", "s1")
    monkeypatch.setenv("LEAK_ME", "nope")
    req = _req(tmp_path, ["true"], inherit_env=False, secret_env=["SECRET_ONE"],
               env={"EXPLICIT": "e"})
    env = build_local_env(req)
    assert env["SECRET_ONE"] == "s1"
    assert env["EXPLICIT"] == "e"
    assert "LEAK_ME" not in env
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution_local.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.execution.local'`.

- [ ] **Step 3: Write `env.py`**

```python
# maestro/execution/env.py
"""Environment assembly for local execution.

`inherit_env=True` reproduces the legacy `spawn_env()` /
`{**os.environ, **child_env()}` idiom exactly (Phase-0 zero-change). When
False, only an explicit allowlist reaches the child — the basis for the SSH
secret contract in later phases.
"""

import os

from maestro._vendor.obs import child_env
from maestro.execution.models import ExecutionRequest


def build_local_env(req: ExecutionRequest) -> dict[str, str]:
    """Build the child environment for a local run."""
    if req.inherit_env:
        return {**os.environ, **child_env()}
    allowed = {name: os.environ[name] for name in req.secret_env if name in os.environ}
    return {**allowed, **req.env, **child_env()}
```

- [ ] **Step 4: Write `local.py`**

```python
# maestro/execution/local.py
"""Local execution backend: an asyncio subprocess wrapped as a TaskHandle."""

import asyncio
import os
import shutil
from datetime import UTC, datetime

from maestro.execution.env import build_local_env
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectPolicy,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)

_TAIL_LIMIT = 4000


class LocalTaskHandle:
    """TaskHandle over a local asyncio.subprocess.Process."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        req: ExecutionRequest,
        log_fd: int | None,
        ref: ExecutionHandleRef,
    ) -> None:
        self._proc = proc
        self._req = req
        self._log_fd = log_fd
        self.ref = ref

    @property
    def os_pid(self) -> int | None:
        return self._proc.pid

    def poll(self) -> int | None:
        # asyncio sets returncode via the loop's child watcher; sync + no I/O.
        return self._proc.returncode

    async def wait(self) -> ExecutionResult:
        stdout_tail = ""
        stderr_tail = ""
        timed_out = False
        try:
            if self._req.capture_output:
                out, err = await self._await(self._proc.communicate())
                stdout_tail = _decode_tail(out)
                stderr_tail = _decode_tail(err)
                # Mirror captured output into the log for uniformity.
                self._req.log_path.write_text(
                    (stdout_tail + ("\n" + stderr_tail if stderr_tail else "")),
                    encoding="utf-8",
                )
            else:
                await self._await(self._proc.wait())
        except TimeoutError:
            timed_out = True
            self._proc.kill()
            await self._proc.wait()
        finally:
            self._close_log()
        return ExecutionResult(
            exit_code=None if timed_out else self._proc.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            output_log_path=self._req.log_path,
            timed_out=timed_out,
        )

    async def _await(self, coro):
        if self._req.timeout_seconds is not None:
            return await asyncio.wait_for(coro, timeout=self._req.timeout_seconds)
        return await coro

    async def terminate(self, grace_seconds: float) -> None:
        if self._proc.returncode is not None:
            return
        self._proc.terminate()
        await asyncio.sleep(grace_seconds)
        if self._proc.returncode is None:
            self._proc.kill()
        await self._proc.wait()
        self._close_log()

    async def kill(self) -> None:
        if self._proc.returncode is None:
            self._proc.kill()
        await self._proc.wait()
        self._close_log()

    async def collect(self) -> CollectResult:
        return CollectResult(applied=False, detail="local: no collect needed")

    async def cleanup(self) -> None:
        self._close_log()

    def _close_log(self) -> None:
        if self._log_fd is not None:
            try:
                os.close(self._log_fd)
            except OSError:
                pass
            self._log_fd = None


def _decode_tail(data: bytes | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text[-_TAIL_LIMIT:]


class LocalBackend:
    """Runs an ExecutionRequest as a local asyncio subprocess."""

    id = "local"

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        missing = [t for t in req.required_tools if shutil.which(t) is None]
        return CapabilityResult(ok=not missing, missing_tools=missing)

    async def run(self, req: ExecutionRequest) -> LocalTaskHandle:
        env = build_local_env(req)
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
                *req.argv,
                cwd=req.workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE if req.stdin is not None else None,
                stdout=stdout,
                stderr=stderr,
            )
        except BaseException:
            if log_fd is not None:
                os.close(log_fd)
            raise
        if req.stdin is not None and proc.stdin is not None:
            proc.stdin.write(req.stdin.encode("utf-8"))
            proc.stdin.close()
        ref = ExecutionHandleRef(
            backend_id=self.id,
            run_id=req.run_id,
            transport_ref=f"local_pid:{proc.pid}",
            started_at=datetime.now(UTC),
        )
        return LocalTaskHandle(proc, req, log_fd, ref)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        if not ref.transport_ref.startswith("local_pid:"):
            return ProbeResult(alive=False, detail="not a local ref")
        pid = int(ref.transport_ref.split(":", 1)[1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProbeResult(alive=False)
        except PermissionError:
            return ProbeResult(alive=True, detail="exists (EPERM)")
        return ProbeResult(alive=True)


_UNUSED = CollectPolicy  # keep import for re-export symmetry; removed if noqa flags
```

Note: delete the trailing `_UNUSED` line if ruff flags it; it exists only to
keep `CollectPolicy` referenced if you re-export. Prefer removing it and the
`CollectPolicy` import if unused.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution_local.py -v`
Expected: PASS (8 tests). If `test_timeout_kills_and_flags` is flaky on a slow
box, it still asserts `timed_out is True` deterministically (1s timeout vs 5s
sleep).

- [ ] **Step 6: Lint + type-check**

Run: `uv run ruff format . && uv run ruff check maestro/execution && uv run pyrefly check`
Expected: no errors (remove `_UNUSED`/unused imports if flagged).

- [ ] **Step 7: Commit**

```bash
git add maestro/execution/env.py maestro/execution/local.py tests/test_execution_local.py
git commit -m "feat(execution): add LocalBackend and LocalTaskHandle"
```

---

## Task 4: Spawner `build_request()` + `can_build_request()`

**Files:**
- Modify: `maestro/spawners/base.py`
- Modify: `maestro/spawners/claude_code.py`, `codex.py`, `opencode.py`,
  `aider.py`, `announce.py`
- Test: `tests/test_spawners.py` (append golden-argv tests)

**Interfaces:**
- Consumes: `ExecutionRequest`, `CollectPolicy` (Task 1).
- Produces on `AgentSpawner` (and every subclass):
  - `build_request(self, task, context, workdir, log_file, run_id, retry_context="", *, model=None) -> ExecutionRequest`
  - `can_build_request(self) -> bool` (local prompt/config validity; **not** a
    tool-availability check).
- `spawn()`/`is_available()` remain (removed in Task 7).

**Design:** `build_request` returns the **same argv** the current `spawn()`
builds, with `inherit_env=True` (reproducing `spawn_env()`), `capture_output=False`,
`collect=CollectPolicy(mode="none")`, and `required_tools=[<cli>]`. The
backend, not the spawner, opens the log and spawns.

- [ ] **Step 1: Write the failing golden-argv tests**

```python
# append to tests/test_spawners.py
from pathlib import Path

from maestro.execution.models import ExecutionRequest
from maestro.spawners.aider import AiderSpawner
from maestro.spawners.announce import AnnounceSpawner
from maestro.spawners.claude_code import ClaudeCodeSpawner
from maestro.spawners.codex import CodexSpawner
from maestro.spawners.opencode import OpencodeSpawner


def _mk_task():
    from maestro.models import AgentType, Task
    return Task(
        id="t1", title="T", prompt="do it", workdir="/tmp/wd",
        agent_type=AgentType.CLAUDE_CODE, scope=["a.py"],
    )


def test_claude_build_request_argv(monkeypatch):
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "claude-sonnet-5")
    req = ClaudeCodeSpawner().build_request(
        _mk_task(), context="ctx", workdir=Path("/tmp/wd"),
        log_file=Path("/tmp/wd/t1.log"), run_id="run-1",
    )
    assert isinstance(req, ExecutionRequest)
    assert req.argv[0] == "claude"
    assert req.argv[1:5] == ["--model", "claude-sonnet-5", "--print"]
    assert req.argv[-2] == "-p"
    assert req.argv[-1].startswith("Task: T")
    assert req.inherit_env is True
    assert req.capture_output is False
    assert req.collect.mode == "none"
    assert req.required_tools == ["claude"]
    assert req.workdir == Path("/tmp/wd")
    assert req.log_path == Path("/tmp/wd/t1.log")


def test_announce_build_request_argv():
    req = AnnounceSpawner().build_request(
        _mk_task(), context="ctx", workdir=Path("/tmp/wd"),
        log_file=Path("/tmp/wd/t1.log"), run_id="run-1",
    )
    assert req.argv[0] == "echo"
    assert req.argv[1].startswith("Task: T")
    assert req.required_tools == []  # echo is a shell builtin/coreutil; no gate


def test_aider_build_request_appends_scope():
    req = AiderSpawner().build_request(
        _mk_task(), context="ctx", workdir=Path("/tmp/wd"),
        log_file=Path("/tmp/wd/t1.log"), run_id="run-1",
    )
    assert req.argv[0] == "aider"
    assert req.argv[-1] == "a.py"  # scope file appended last
    assert req.required_tools == ["aider"]


def test_can_build_request_true():
    assert ClaudeCodeSpawner().can_build_request() is True
    assert AnnounceSpawner().can_build_request() is True
    assert CodexSpawner().can_build_request() is True
    assert OpencodeSpawner().can_build_request() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spawners.py -k build_request -v`
Expected: FAIL — `AttributeError: 'ClaudeCodeSpawner' object has no attribute 'build_request'`.

- [ ] **Step 3: Add abstract methods to the ABC**

In `maestro/spawners/base.py`, add the import and two methods to `AgentSpawner`
(keep `spawn`/`is_available`):

```python
# add to imports
from maestro.execution.models import CollectPolicy, ExecutionRequest
```

```python
# add inside class AgentSpawner, after spawn(...)
    @abstractmethod
    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        """Build a transport-agnostic ExecutionRequest ('what to run').

        The backend (LocalBackend/SshBackend) owns spawning ('where/how').
        """
        ...

    def can_build_request(self) -> bool:
        """Whether this spawner can build a valid request locally.

        Default True. Override for spawners with local config prerequisites.
        This is NOT a tool-availability check — that is the backend's job
        (`ExecutionBackend.can_run` probes required_tools on the executor).
        """
        return True
```

- [ ] **Step 4: Implement `build_request` in each spawner**

`maestro/spawners/claude_code.py` — add (keep `spawn`):

```python
    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        prompt = self.build_prompt(task, context, retry_context)
        catalog = load_catalog()
        resolved, source = resolve_model(
            model, "MAESTRO_CLAUDE_MODEL", "claude_code", catalog
        )
        _obs_log.info(
            "agent.model_resolved", harness="claude_code",
            model=resolved, source=source,
        )
        warn_on_model_status(resolved, source, catalog)
        return ExecutionRequest(
            run_id=run_id,
            argv=[
                "claude", "--model", resolved, "--print",
                "--output-format", "json", "-p", prompt,
            ],
            workdir=workdir,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
            required_tools=["claude"],
        )
```

Add the import to `claude_code.py`:
```python
from maestro.execution.models import CollectPolicy, ExecutionRequest
```

`maestro/spawners/codex.py` — same shape, argv from its current `spawn`:
```python
    def build_request(self, task, context, workdir, log_file, run_id,
                      retry_context="", *, model=None) -> ExecutionRequest:
        prompt = self.build_prompt(task, context, retry_context)
        catalog = load_catalog()
        resolved, source = resolve_model(
            model, "MAESTRO_CODEX_MODEL", "codex_cli", catalog
        )
        _obs_log.info("agent.model_resolved", harness="codex_cli",
                      model=resolved, source=source)
        warn_on_model_status(resolved, source, catalog)
        return ExecutionRequest(
            run_id=run_id,
            argv=["codex", "exec", "-m", resolved, "--sandbox",
                  "workspace-write", "--skip-git-repo-check", prompt],
            workdir=workdir, log_path=log_file, inherit_env=True,
            collect=CollectPolicy(mode="none"), required_tools=["codex"],
        )
```
(Add the same `from maestro.execution.models import CollectPolicy, ExecutionRequest`
import and full type hints matching the ABC signature.)

`maestro/spawners/opencode.py`:
```python
    def build_request(self, task, context, workdir, log_file, run_id,
                      retry_context="", *, model=None) -> ExecutionRequest:
        prompt = self.build_prompt(task, context, retry_context)
        catalog = load_catalog()
        resolved, source = resolve_model(
            model, "MAESTRO_OPENCODE_MODEL", "opencode", catalog
        )
        _obs_log.info("agent.model_resolved", harness="opencode",
                      model=resolved, source=source)
        warn_on_model_status(resolved, source, catalog)
        return ExecutionRequest(
            run_id=run_id,
            argv=["opencode", "run", "--format", "json", "-m",
                  _qualify(resolved), prompt],
            workdir=workdir, log_path=log_file, inherit_env=True,
            collect=CollectPolicy(mode="none"), required_tools=["opencode"],
        )
```

`maestro/spawners/aider.py`:
```python
    def build_request(self, task, context, workdir, log_file, run_id,
                      retry_context="", *, model=None) -> ExecutionRequest:
        prompt = self.build_prompt(task, context, retry_context)
        argv = ["aider", "--yes-always", "--no-auto-commits", "--message", prompt]
        if task.scope:
            argv.extend(task.scope)
        return ExecutionRequest(
            run_id=run_id, argv=argv, workdir=workdir, log_path=log_file,
            inherit_env=True, collect=CollectPolicy(mode="none"),
            required_tools=["aider"],
        )
```

`maestro/spawners/announce.py` (no `required_tools` — `echo` is not gated;
`inherit_env=True` is a harmless superset of today's bare-inherit):
```python
    def build_request(self, task, context, workdir, log_file, run_id,
                      retry_context="", *, model=None) -> ExecutionRequest:
        prompt = self.build_prompt(task, context, retry_context)
        return ExecutionRequest(
            run_id=run_id, argv=["echo", prompt], workdir=workdir,
            log_path=log_file, inherit_env=True,
            collect=CollectPolicy(mode="none"),
        )
```

- [ ] **Step 5: Run the golden tests + full spawner suite**

Run: `uv run pytest tests/test_spawners.py -v`
Expected: PASS (existing `spawn` tests still pass; new `build_request` tests
pass).

- [ ] **Step 6: Lint + type-check**

Run: `uv run ruff format . && uv run ruff check maestro/spawners && uv run pyrefly check`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add maestro/spawners tests/test_spawners.py
git commit -m "feat(spawners): add build_request/can_build_request (backend-agnostic)"
```

---

## Task 5: Wire the scheduler onto LocalBackend

**Files:**
- Modify: `maestro/scheduler.py` (`RunningTask`, `SpawnerProtocol`, spawn call
  `979-1008`, poll `1100-1105`, timeout `1439-1448`, cleanup `1542-1550`,
  availability `917-925`)
- Test: `tests/test_scheduler.py` (fakes `Popen` → fake handle)

**Interfaces:**
- Consumes: `LocalBackend`, `LocalTaskHandle` (Task 3); `build_request`,
  `can_build_request` (Task 4); `TaskHandle` (Task 2).
- Produces: `RunningTask.handle: TaskHandle` (replaces `.process`). The
  scheduler now drives the run via `handle.poll()/terminate()/kill()/wait()`.

**Design:** Add `self._backend = LocalBackend()` in `__init__`. Replace the
`spawner.spawn(...)` call with `spawner.build_request(...)` +
`await self._backend.run(req)`. Availability preflight becomes
`spawner.can_build_request()` + `await self._backend.can_run(req)`. `poll()`
stays sync; `terminate/kill/wait` become awaited handle calls (the scheduler is
already async at all three sites).

- [ ] **Step 1: Update the test fakes to fail first**

In `tests/test_scheduler.py`, the fake spawner currently returns a fake `Popen`.
Add a fake handle and switch the fake spawner to `build_request`. Example
replacement fake (adapt to the file's existing fixture names):

```python
class _FakeHandle:
    def __init__(self, exit_code=0):
        self._code = exit_code
        self._polled = False
        from datetime import UTC, datetime
        from maestro.execution.models import ExecutionHandleRef, ExecutionResult
        self.ref = ExecutionHandleRef(
            backend_id="local", run_id="r", transport_ref="local_pid:1",
            started_at=datetime.now(UTC),
        )
        self._ExecutionResult = ExecutionResult

    @property
    def os_pid(self): return 1
    def poll(self):
        # first tick: running; then finished (mimics real process lifecycle)
        code, self._polled = (self._code if self._polled else None), True
        return code
    async def wait(self):
        from pathlib import Path
        return self._ExecutionResult(exit_code=self._code, output_log_path=Path("/tmp/x"))
    async def terminate(self, grace_seconds): ...
    async def kill(self): ...
    async def collect(self):
        from maestro.execution.models import CollectResult
        return CollectResult(applied=False)
    async def cleanup(self): ...
```

Add a test asserting the scheduler stores a handle, not a Popen:

```python
async def test_running_task_holds_handle(scheduler_with_fake_backend):
    # after a task starts, the RunningTask should expose `.handle`
    sched = scheduler_with_fake_backend
    # ... start one task via the existing harness ...
    running = next(iter(sched._running_tasks.values()))
    assert hasattr(running, "handle")
    assert not hasattr(running, "process")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scheduler.py -k handle -v`
Expected: FAIL — `RunningTask` has `.process`, not `.handle`.

- [ ] **Step 3: Change `RunningTask`**

`maestro/scheduler.py:140-154`:

```python
@dataclass
class RunningTask:
    """Represents a currently running task with its execution handle.

    Attributes:
        task: The task being executed.
        handle: Execution handle (poll/wait/terminate/kill/collect/cleanup).
        started_at: When the task started.
        log_file: Path to the log file.
    """

    task: Task
    handle: TaskHandle
    started_at: datetime
    log_file: Path
```

Add imports near the top of `scheduler.py`:
```python
from maestro.execution.backend import TaskHandle
from maestro.execution.local import LocalBackend
```

In `Scheduler.__init__`, after the spawner/validator setup, add:
```python
        self._backend = LocalBackend()
```

- [ ] **Step 4: Replace the spawn call site (`979-1008`)**

```python
            routed_model = (
                model_of_agent_id(task.routed_agent_type)
                if task.routed_agent_type
                else None
            )
            if task.routed_agent_type and routed_model == "":
                _obs_log.warning(
                    "agent.routed_model_empty",
                    task_id=task_id,
                    agent_id=task.routed_agent_type,
                )
            request = spawner.build_request(
                task, context, workdir, log_file, task_id, retry_context,
                model=routed_model,
            )
            handle = await self._backend.run(request)

            self._running_tasks[task_id] = RunningTask(
                task=task,
                handle=handle,
                started_at=datetime.now(UTC),
                log_file=log_file,
            )
```

- [ ] **Step 5: Update availability preflight (`917-925`)**

```python
        spawner = self._spawners.get(spawner_key)
        if spawner is None:
            msg = f"No spawner available for agent type '{spawner_key}'"
            raise SchedulerError(msg)

        if not spawner.can_build_request():
            msg = f"Agent '{spawner_key}' cannot build a request (local config)"
            raise SchedulerError(msg)
```

The executor-side tool probe (`backend.can_run`) is exercised in Phase 2; for
`LocalBackend` in Phase 0 it always passes for installed CLIs, so keeping only
`can_build_request()` here preserves today's semantics (a missing local CLI now
surfaces as a spawn failure at `backend.run`, classified by the existing
failure path — see Step 6 note). To retain the *early* "not available" error,
optionally add:

```python
        # optional early probe (keeps today's fail-fast behavior)
        probe_req = spawner.build_request(
            task, "", workdir, log_file, task_id
        )
        cap = await self._backend.can_run(probe_req)
        if not cap.ok:
            msg = f"Agent '{spawner_key}' missing tools: {cap.missing_tools}"
            raise SchedulerError(msg)
```

Prefer the optional probe form to keep `test_scheduler.py`'s "agent not
available raises SchedulerError" assertions valid. (Announce has no
`required_tools`, so it is never blocked — matching `is_available() == True`.)

- [ ] **Step 6: Update poll/terminate/kill/wait sites**

`scheduler.py:1100-1105`:
```python
            return_code = running_task.handle.poll()
            if return_code is not None:
                await self._handle_task_completion(task_id, running_task, return_code)
```

`scheduler.py:1439-1448` (timeout) → replace the sync Popen block with:
```python
            await running_task.handle.terminate(self._config.shutdown_grace_seconds)
```

`scheduler.py:1542-1550` (cleanup) → replace with:
```python
                await running_task.handle.terminate(self._config.shutdown_grace_seconds)
```

(`LocalTaskHandle.terminate` already does terminate → grace sleep → kill → reap,
so the multi-line sequences collapse into one awaited call.)

- [ ] **Step 7: Run the scheduler suite**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: PASS. Fix any remaining fake-`Popen` references in the test file by
switching them to `_FakeHandle` and pointing the scheduler's `self._backend` at
a fake backend whose `run()` returns the fake handle (inject via the existing
fixture, or monkeypatch `sched._backend`).

- [ ] **Step 8: Lint + type-check + full suite**

Run: `uv run ruff format . && uv run ruff check maestro/scheduler.py && uv run pyrefly check && uv run pytest -q`
Expected: green.

- [ ] **Step 9: Commit**

```bash
git add maestro/scheduler.py tests/test_scheduler.py
git commit -m "refactor(scheduler): drive tasks via ExecutionBackend/TaskHandle"
```

---

## Task 6: Wire the orchestrator onto LocalBackend

**Files:**
- Modify: `maestro/orchestrator.py` (`RunningWorkstream` `95-103`, spawn
  `649-671`, register/pid `677-702`, monitor `790-795`, cleanup `1190-1202`)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `LocalBackend`, `TaskHandle`, `ExecutionRequest`, `CollectPolicy`.
- Produces: `RunningWorkstream.handle: TaskHandle` (replaces `.process`). PID for
  recovery comes from `handle.os_pid`.

**Design:** Build an `ExecutionRequest` for the `spec-runner run` command and
run it through `self._backend` (add `self._backend = LocalBackend()` in
`__init__`). `process.returncode` → `handle.poll()`; `process.pid` →
`handle.os_pid`; terminate/kill/wait → `handle.terminate(...)`.

- [ ] **Step 1: Add a failing test**

```python
# tests/test_orchestrator.py
async def test_running_workstream_holds_handle(orchestrator_with_fake_backend):
    orch = orchestrator_with_fake_backend
    # ... start one workstream via existing harness ...
    running = next(iter(orch._running.values()))
    assert hasattr(running, "handle")
    assert not hasattr(running, "process")
    assert running.handle.os_pid is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_orchestrator.py -k handle -v`
Expected: FAIL — `RunningWorkstream` has `.process`.

- [ ] **Step 3: Change `RunningWorkstream` (`95-103`)**

```python
@dataclass
class RunningWorkstream:
    """Represents a currently running workstream execution."""

    workstream: Workstream
    handle: TaskHandle
    started_at: datetime
    workspace_path: Path
    log_file: Path
```

Add import:
```python
from maestro.execution.backend import TaskHandle
from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest
```

In `Orchestrator.__init__`, add:
```python
        self._backend = LocalBackend()
```

- [ ] **Step 4: Replace the spec-runner spawn (`649-695`)**

```python
        log_file = self._log_dir / f"{workstream_id}.log"

        cmd = ["spec-runner", "run", "--all", "--spec-prefix", SPEC_PREFIX]
        if self._config.callback_url:
            cmd.extend(["--callback-url", self._config.callback_url])

        request = ExecutionRequest(
            run_id=workstream_id,
            argv=cmd,
            workdir=workspace,
            log_path=log_file,
            inherit_env=True,
            collect=CollectPolicy(mode="none"),
            required_tools=["spec-runner"],
        )
        with span("task.execute", task_id=workstream_id):
            handle = await self._backend.run(request)

        self._running[workstream_id] = RunningWorkstream(
            workstream=workstream.model_copy(
                update={
                    "status": WorkstreamStatus.RUNNING,
                    "workspace_path": str(workspace),
                }
            ),
            handle=handle,
            started_at=datetime.now(UTC),
            workspace_path=workspace,
            log_file=log_file,
        )

        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.RUNNING,
            process_pid=handle.os_pid,
        )
        self._logger.info(
            "Spawned spec-runner for '%s' (PID %s) in %s",
            workstream_id,
            handle.os_pid,
            workspace,
        )
```

(The `log_fd`/`os.open` block is deleted — `LocalBackend.run` owns the log fd.)

- [ ] **Step 5: Update monitor + cleanup sites**

`orchestrator.py:790-795`:
```python
            return_code = running.handle.poll()
            if return_code is not None:
                await self._handle_completion(zid, running, return_code)
                completed.append(zid)
```

`orchestrator.py:1190-1202` (cleanup):
```python
        for zid, running in list(self._running.items()):
            try:
                await running.handle.terminate(self._shutdown_grace_seconds)
            except OSError as e:
                self._logger.debug(
                    "Failed to terminate process for workstream %s during cleanup: %s",
                    zid, e,
                )
```

- [ ] **Step 6: Run the orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS. Update any fake that returned an `asyncio.subprocess.Process`
to return a fake handle (same `_FakeHandle` shape as Task 5, with
`os_pid` set), and point `orch._backend` at a fake backend.

- [ ] **Step 7: Lint + type-check + full suite**

Run: `uv run ruff format . && uv run ruff check maestro/orchestrator.py && uv run pyrefly check && uv run pytest -q`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "refactor(orchestrator): drive workstreams via ExecutionBackend/TaskHandle"
```

---

## Task 7: Remove dead spawn paths + consolidate env

**Files:**
- Modify: `maestro/spawners/base.py` (drop `spawn`, `spawn_env`, `is_available`
  abstractness), all five spawners (drop `spawn`/`is_available`),
  `maestro/scheduler.py` (drop `SpawnerProtocol.spawn`/`is_available`, and the
  deprecated `BaseSpawner` if now unused).
- Test: `tests/test_spawners.py`, `tests/test_scheduler.py` (drop `spawn`-based
  tests superseded by `build_request` golden tests).

**Design:** Now that no consumer calls `spawn()`/`is_available()`, delete them so
there is one code path. Keep `spawn_env()` only if something outside spawners
still imports it; otherwise remove it (its logic now lives in
`build_local_env` with `inherit_env=True`).

- [ ] **Step 1: Find remaining references**

Run:
```bash
grep -rn "\.spawn(" maestro/ tests/ | grep -v build_request
grep -rn "is_available\|spawn_env\|BaseSpawner" maestro/ tests/
```
Expected: only definitions remain (no live call sites in `maestro/`).

- [ ] **Step 2: Delete `spawn`/`is_available` from the ABC and subclasses**

Remove the `@abstractmethod def spawn(...)` and `@abstractmethod def
is_available(...)` from `maestro/spawners/base.py`, and the concrete `spawn`/
`is_available` from `claude_code.py`, `codex.py`, `opencode.py`, `aider.py`,
`announce.py`. Remove now-unused imports (`from subprocess import Popen`,
`import subprocess`, `import os`, `shutil` where only used by the deleted code).
Remove `spawn_env()` if `grep` shows no importer.

In `maestro/scheduler.py`, drop `spawn`/`is_available` from `SpawnerProtocol`
(keep `agent_type`, add `build_request`/`can_build_request`), and delete the
deprecated `BaseSpawner(ABC)` block (`92-137`) if unreferenced:

```python
class SpawnerProtocol(Protocol):
    """Protocol for agent spawners."""

    @property
    def agent_type(self) -> str: ...

    def can_build_request(self) -> bool: ...

    def build_request(
        self, task: Task, context: str, workdir: Path, log_file: Path,
        run_id: str, retry_context: str = "", *, model: str | None = None,
    ) -> ExecutionRequest: ...
```

- [ ] **Step 3: Delete superseded tests**

Remove `spawn`-based tests in `tests/test_spawners.py` (those constructing a
`Popen` from `spawner.spawn(...)`) — the `build_request` golden tests (Task 4)
plus `tests/test_execution_local.py` (which proves the argv actually runs) now
cover them. Keep prompt-building and registry tests.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: green. Coverage still ≥ 80.

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: no errors (no unused imports left behind).

- [ ] **Step 6: Zero-behavior-change acceptance check**

Confirm no `execution` config exists in any shipped example and the default
path is `local`:
```bash
grep -rn "execution:" examples/ || echo "no execution config in examples (expected)"
uv run pytest -q
```
Expected: examples unchanged; full suite green — proving `local + bare` is the
unconfigured default.

- [ ] **Step 7: Commit**

```bash
git add maestro tests
git commit -m "refactor(spawners): remove legacy spawn()/is_available() paths"
```

---

## Self-Review (completed against the spec)

- **Contract (spec §1):** Tasks 1–2 create every model + both Protocols,
  including `progress_mirror`, `secret_env`, `status_marker`, mirror paths
  (unused in Phase 0 but part of the frozen contract). ✓
- **LocalBackend (spec §2, Phase 0):** Task 3, standalone-tested. ✓
- **Availability split (spec §3):** Task 4 (`can_build_request`) + Task 5
  (`backend.can_run`). ✓
- **poll() sync + cached (spec §3 note):** `LocalTaskHandle.poll()` returns
  `proc.returncode`, no I/O. ✓
- **Spawner seam (spec §6.2):** Task 4 `build_request`. ✓
- **Scheduler/orchestrator migration (spec §6.1/§6.3, Phase 0):** Tasks 5–6. ✓
- **Env/secret split (spec §10):** `build_local_env` with `inherit_env=True`
  reproduces `spawn_env()` exactly; allowlist path tested for later phases. ✓
- **Recovery pid parity (spec §12):** `handle.os_pid` feeds `process_pid`;
  `LocalBackend.probe` uses `os.kill(pid, 0)`. ✓
- **Zero behavior change (MVP guarantee):** every task ends with the full suite
  green; Task 7 Step 6 asserts the unconfigured default is `local + bare`. ✓

**Deferred (not Phase 0, tracked in spec):** validator rewiring through the
backend (spec §9) → **Phase 2a**, when `validation_backend: same` first needs a
non-local backend; Phase 0 leaves `Validator` running locally (behavior
unchanged) and proves the `capture_output` contract via
`tests/test_execution_local.py`. Docker/SSH/collect/progress-mirror → Phases
1–2c.

## Non-Goals (Phase 0)

- No SSH, Docker, rsync, remote hosts, or `ContainerRuntime` vendoring.
- No `collect`/`progress_mirror` behavior — `LocalTaskHandle.collect()` is a
  no-op; `progress_mirror` is ignored by `LocalBackend`.
- No `Validator` rewiring (deferred to Phase 2a).
- No config parsing/routing surface — `LocalBackend` is instantiated directly;
  the `execution` YAML block (spec §13) lands with the first non-local backend.
- No new pytest markers (kept out to respect `--strict-markers`).
