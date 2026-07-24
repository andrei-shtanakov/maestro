# SSH Execution Backend (Phase 2a, Mode 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Mode-2 (Orchestrator) spec-runner workstreams on a remote host over SSH — rsync the worktree out, launch spec-runner detached via a daemonizing Python supervisor, mirror live progress back (WAL-safe), collect final changes into the worktree before ex-post gates, and recover safely across a center restart.

**Architecture:** SSH is a *new transport* (not a `LocalBackend` isolator): `SshBackend` is a peer `ExecutionBackend` whose `run()` starts a local asyncio monitor task tailing a remote atomic status marker; `poll()` returns that cache. The `execution` config becomes a named `backends:{}` registry (transport × isolation), with the Phase-1 `docker:` field kept as a legacy shim. A durable `collected` execution-handle state and callback-driven `finalize_handle` make `terminal → collected → cleaned` observable so a crash never loses uncollected remote changes.

**Tech Stack:** Python 3.12+, uv, pydantic, aiosqlite (SQLite WAL), asyncio subprocess, OpenSSH (`ssh`/`rsync`), a shipped Python supervisor resource, pytest + anyio.

## Global Constraints

- Package manager: **uv only** (`uv add`, `uv run`); never pip. Line length **88**. Type hints on all code; `uv run pyrefly check` clean. `uv run ruff format .` + `uv run ruff check .` clean. Public APIs get docstrings. f-strings for formatting.
- **No `execution` config → `local + bare`, behavior-compatible with today** (not byte-identical: the registry refactor + migration #8 change internal representation only).
- **A remote executor never receives GitHub credentials.** `GH_TOKEN` / `GITHUB_TOKEN` / `GH_*` denylisted on every `secret_env`; git/PR/merge stays on the center.
- **`spec-runner plan --full` (generation) stays local.**
- **Mode 1 is out of scope.** Selecting an `ssh`-transport backend from a Mode-1 config **fails fast** ("SSH backends are Mode-2 only until Phase 2b").
- Isolation for SSH in this phase is **`bare` only** (SSH + Docker is Phase 2c).
- **Verification discipline (operational learning):** verify with **targeted foreground** runs (single files / `-k` halves) + `pyrefly check` + `ruff`. **Never** offload the whole suite to a background wait — a workspace watchdog kills long background `pytest` runs. Rely on PR CI for the full suite.
- Secrets never appear in any `ssh`/`rsync`/process argv or logs; delivered only via a `0600` env-file in a `0700` dir.
- Spec of record: `docs/superpowers/specs/2026-07-24-maestro-ssh-backend-phase2a-design.md`. Where this plan and the spec disagree, stop and reconcile before coding.

## File Structure

**New files:**
- `maestro/execution/secret_file.py` — shared `0600`/`0700` env-file writer + control-char validation (extracted from `DockerIsolator.materialize`).
- `maestro/execution/ssh_cli.py` — `SshCli`: injectable-runner wrapper over `ssh`/`rsync` with the security-options whitelist, host/user/port rendering, tool probe.
- `maestro/execution/resources/maestro_supervisor.py` — the fixed, versioned remote supervisor (daemonizes, owns the workload, writes the atomic status marker). Shipped as package data.
- `maestro/execution/ssh_launch.py` — pure builders: launch-descriptor JSON, git-bundle plan, rsync include/exclude sets, remote path layout.
- `maestro/execution/ssh_collect.py` — pre-run baseline + two-phase transactional collect (preflight + rollback journal).
- `maestro/execution/ssh_mirror.py` — WAL-safe progress mirror driver (`sqlite3.backup()` snapshot over stdin).
- `maestro/execution/ssh_handle.py` — `SshTaskHandle` + the local monitor task.
- `maestro/execution/ssh_backend.py` — `SshBackend` (`healthcheck`/`can_run`/`run`/`probe`).
- `maestro/execution/ssh_recovery.py` — SSH probe/GC classification (peer of `docker_recovery.py`).
- `examples/with-ssh.yaml` — a Mode-2 project.yaml using an `ssh` backend.

**Modified files:**
- `maestro/execution/exec_config.py` — `BackendSpec`/`TransportSpec`/`IsolationSpec`, registry `ExecutionConfig`, legacy-docker normalization, host-field + secret validators.
- `maestro/execution/resolver.py` — build backends from the registry; Mode-2-only SSH guard.
- `maestro/execution/finalize.py` — callback-driven `finalize_handle(on_terminal, on_collected)`; `FinalizationResult` gains `collect_succeeded`/`cleanup_attempted`.
- `maestro/execution/isolators.py` — `DockerIsolator.materialize` uses `secret_file.py`.
- `maestro/database.py` — migration #8 (`collected` state + `remote_host`/`remote_dir`/`status_marker`/`collected_at` columns); `start_execution` persists them; `mark_execution_state` allows `collected`; `get_open_execution_handles` widened to `collected`.
- `maestro/orchestrator.py` — SSH request wiring (collect + progress_mirror), mirror dir into `_update_progress`, finalize callbacks, gate on `collect_succeeded`, SSH recovery branch.
- `maestro/recovery.py` — (unchanged behavior; docker-only) — no SSH in Mode 1.
- `maestro/CLAUDE.md` — drift note: polling reintroduced for remote executors.
- `pyproject.toml` — ensure `maestro/execution/resources/*.py` ships as package data.

---

## Increment A — Config registry + legacy Docker shim

### Task 1 (A1): Transport / isolation / backend config models

**Files:**
- Modify: `maestro/execution/exec_config.py`
- Test: `tests/test_exec_config_registry.py` (create)

**Interfaces:**
- Consumes: nothing (leaf config models).
- Produces:
  - `LocalTransport(type: Literal["local"])`
  - `SshTransport(type: Literal["ssh"], host: str, user: str | None = None, port: int | None = None, workdir_root: str, connect_timeout_s: int = 10, ssh_opts: list[str] = [])`
  - `BareIsolation(type: Literal["bare"])`
  - `DockerIsolation(type: Literal["docker"], image: str, network: str = "none", memory: str | None, cpus: str | None, user: str | None)`
  - `BackendSpec(transport: LocalTransport | SshTransport, isolation: BareIsolation | DockerIsolation, secret_env: list[str] = [], inherit_secret_defaults: bool = False, max_concurrent: int | None = None)`
  - module fn `is_denylisted(name: str) -> bool` (kept; re-exported).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exec_config_registry.py
import pytest
from pydantic import ValidationError

from maestro.execution.exec_config import (
    BackendSpec,
    SshTransport,
)


def test_ssh_transport_rejects_host_with_user_or_port_token():
    with pytest.raises(ValidationError):
        SshTransport(type="ssh", host="alice@gpu-box", workdir_root="/var/tmp/m")
    with pytest.raises(ValidationError):
        SshTransport(type="ssh", host="gpu-box:22", workdir_root="/var/tmp/m")


def test_ssh_transport_accepts_bare_host_with_separate_user_port():
    t = SshTransport(
        type="ssh", host="gpu-box", user="alice", port=2222, workdir_root="/var/tmp/m"
    )
    assert (t.host, t.user, t.port) == ("gpu-box", "alice", 2222)


def test_backend_spec_secret_env_rejects_github_creds():
    with pytest.raises(ValidationError):
        BackendSpec(
            transport=SshTransport(type="ssh", host="h", workdir_root="/w"),
            isolation={"type": "bare"},
            secret_env=["ANTHROPIC_API_KEY", "GH_TOKEN"],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exec_config_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'BackendSpec'`.

- [ ] **Step 3: Write minimal implementation**

Add to `maestro/execution/exec_config.py` (keep the existing `DockerConfig`/`ExecutionConfig` for now — Task A2 rewrites `ExecutionConfig`):

```python
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def is_denylisted(name: str) -> bool:
    """True if `name` is a GitHub credential env var (exact or GH_* prefix)."""
    return name in {"GH_TOKEN", "GITHUB_TOKEN"} or name.startswith("GH_")


class LocalTransport(BaseModel):
    type: Literal["local"] = "local"


class SshTransport(BaseModel):
    type: Literal["ssh"]
    host: str
    user: str | None = None
    port: int | None = None
    workdir_root: str
    connect_timeout_s: int = 10
    ssh_opts: list[str] = Field(default_factory=list)

    @field_validator("host")
    @classmethod
    def _reject_composite_host(cls, value: str) -> str:
        if "@" in value or ":" in value:
            msg = f"host must be a bare hostname/alias; use user/port fields: {value!r}"
            raise ValueError(msg)
        if not value.strip():
            raise ValueError("host must not be empty")
        return value


class BareIsolation(BaseModel):
    type: Literal["bare"] = "bare"


class DockerIsolation(BaseModel):
    type: Literal["docker"]
    image: str
    network: str = "none"
    memory: str | None = None
    cpus: str | None = None
    user: str | None = None


class BackendSpec(BaseModel):
    transport: LocalTransport | SshTransport = Field(discriminator="type")
    isolation: BareIsolation | DockerIsolation = Field(discriminator="type")
    secret_env: list[str] = Field(default_factory=list)
    inherit_secret_defaults: bool = False
    max_concurrent: int | None = None

    @field_validator("secret_env")
    @classmethod
    def _reject_gh(cls, value: list[str]) -> list[str]:
        bad = [n for n in value if is_denylisted(n)]
        if bad:
            raise ValueError(f"secret_env may not carry GitHub credentials: {bad}")
        return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_exec_config_registry.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/exec_config.py tests/test_exec_config_registry.py
git commit -m "feat(exec): transport/isolation/backend config models for registry"
```

---

### Task 2 (A2): Registry `ExecutionConfig` + legacy-docker normalization

**Files:**
- Modify: `maestro/execution/exec_config.py`
- Test: `tests/test_exec_config_registry.py`

**Interfaces:**
- Consumes: A1 models; existing `DockerConfig` (Phase-1).
- Produces:
  - `ExecutionConfig(default_backend: str = "local", secret_env_defaults: list[str] = [], backends: dict[str, BackendSpec] = {}, docker: DockerConfig | None = None)`
  - `ExecutionConfig.normalized() -> dict[str, BackendSpec]` — resolves the effective registry: `local` implicit; legacy `docker` shimmed into `backends["docker"]`; raises `ValueError` on legacy+canonical `docker` collision.
  - `ExecutionConfig.effective_secret_env(name: str) -> list[str]` — per-backend allowlist, unioned with `secret_env_defaults` iff `inherit_secret_defaults`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_exec_config_registry.py
from maestro.execution.exec_config import DockerConfig, ExecutionConfig


def test_local_is_implicit_backend():
    cfg = ExecutionConfig()
    reg = cfg.normalized()
    assert "local" in reg and reg["local"].transport.type == "local"


def test_legacy_docker_is_shimmed_into_registry():
    cfg = ExecutionConfig(docker=DockerConfig(image="img:1", secret_env=["ANTHROPIC_API_KEY"]))
    reg = cfg.normalized()
    assert reg["docker"].isolation.type == "docker"
    assert reg["docker"].isolation.image == "img:1"
    assert reg["docker"].secret_env == ["ANTHROPIC_API_KEY"]
    assert reg["docker"].transport.type == "local"


def test_legacy_and_canonical_docker_collision_raises():
    cfg = ExecutionConfig(
        docker=DockerConfig(image="img:1"),
        backends={
            "docker": BackendSpec(
                transport={"type": "local"},
                isolation={"type": "docker", "image": "img:2"},
            )
        },
    )
    with pytest.raises(ValueError, match="docker"):
        cfg.normalized()


def test_effective_secret_env_inherits_defaults_only_when_flagged():
    cfg = ExecutionConfig(
        secret_env_defaults=["ANTHROPIC_API_KEY"],
        backends={
            "a": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/w"),
                isolation={"type": "bare"},
                secret_env=["OPENAI_API_KEY"],
                inherit_secret_defaults=True,
            ),
            "b": BackendSpec(
                transport=SshTransport(type="ssh", host="h2", workdir_root="/w"),
                isolation={"type": "bare"},
                secret_env=["OPENAI_API_KEY"],
            ),
        },
    )
    assert set(cfg.effective_secret_env("a")) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    assert cfg.effective_secret_env("b") == ["OPENAI_API_KEY"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exec_config_registry.py -k "shimmed or implicit or collision or inherits" -v`
Expected: FAIL — `normalized`/`effective_secret_env` do not exist.

- [ ] **Step 3: Write minimal implementation**

Replace the Phase-1 `ExecutionConfig` in `maestro/execution/exec_config.py` with:

```python
class ExecutionConfig(BaseModel):
    """The `execution:` block. Absent → `local + bare`, old behavior.

    `backends` is the canonical named registry (transport × isolation). The
    Phase-1 `docker` field is a legacy shorthand normalized into
    `backends["docker"]`. `local` is always implicit.
    """

    default_backend: str = "local"
    secret_env_defaults: list[str] = Field(default_factory=list)
    backends: dict[str, BackendSpec] = Field(default_factory=dict)
    docker: DockerConfig | None = None

    def normalized(self) -> dict[str, "BackendSpec"]:
        """Effective registry: implicit `local`, legacy-docker shim, no collision."""
        registry: dict[str, BackendSpec] = dict(self.backends)
        if "local" not in registry:
            registry["local"] = BackendSpec(
                transport=LocalTransport(), isolation=BareIsolation()
            )
        if self.docker is not None:
            if "docker" in self.backends:
                raise ValueError(
                    "execution.docker (legacy) and backends.docker (canonical) "
                    "are both set; remove one — no implicit precedence"
                )
            registry["docker"] = BackendSpec(
                transport=LocalTransport(),
                isolation=DockerIsolation(
                    type="docker",
                    image=self.docker.image,
                    network=self.docker.network,
                    memory=self.docker.memory,
                    cpus=self.docker.cpus,
                    user=self.docker.user,
                ),
                secret_env=list(self.docker.secret_env),
            )
        return registry

    def effective_secret_env(self, name: str) -> list[str]:
        """Per-backend secret allowlist, unioning defaults iff opted in."""
        spec = self.normalized()[name]
        if spec.inherit_secret_defaults:
            merged = list(dict.fromkeys([*self.secret_env_defaults, *spec.secret_env]))
            return merged
        return list(spec.secret_env)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_exec_config_registry.py -v`
Expected: PASS (all).

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/exec_config.py tests/test_exec_config_registry.py
git commit -m "feat(exec): registry ExecutionConfig with legacy-docker shim"
```

---

### Task 3 (A3): `BackendResolver` builds from the registry + Mode-2-only SSH guard

**Files:**
- Modify: `maestro/execution/resolver.py`
- Test: `tests/test_backend_resolver_registry.py` (create)

**Interfaces:**
- Consumes: A2 `ExecutionConfig.normalized()`; existing `LocalBackend`, `DockerIsolator`, `DockerCli`; (SSH backend arrives in Task D2 — until then the `ssh` branch raises a clear "not yet wired" only if actually selected, replaced in D2).
- Produces:
  - `BackendResolver(execution: ExecutionConfig | None, *, mode: Literal["scheduler", "orchestrator"] = "orchestrator")`
  - `resolve(name: str | None) -> ExecutionBackend` — unknown name → `ExecutionConfigError`; `ssh` transport in `mode="scheduler"` → `ExecutionConfigError` ("Mode-2 only until Phase 2b").

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend_resolver_registry.py
import pytest

from maestro.execution.exec_config import BackendSpec, ExecutionConfig, SshTransport
from maestro.execution.resolver import BackendResolver, ExecutionConfigError


def _ssh_cfg() -> ExecutionConfig:
    return ExecutionConfig(
        default_backend="local",
        backends={
            "gpu": BackendSpec(
                transport=SshTransport(type="ssh", host="gpu", workdir_root="/w"),
                isolation={"type": "bare"},
            )
        },
    )


def test_local_resolves_with_no_config():
    r = BackendResolver(None)
    assert r.resolve(None).id == "local"


def test_unknown_backend_raises():
    with pytest.raises(ExecutionConfigError, match="unknown"):
        BackendResolver(_ssh_cfg()).resolve("nope")


def test_ssh_backend_rejected_in_scheduler_mode():
    r = BackendResolver(_ssh_cfg(), mode="scheduler")
    with pytest.raises(ExecutionConfigError, match="Mode-2"):
        r.resolve("gpu")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backend_resolver_registry.py -v`
Expected: FAIL — `BackendResolver` has no `mode` kwarg / registry logic.

- [ ] **Step 3: Write minimal implementation**

Rewrite `maestro/execution/resolver.py`:

```python
"""Per-dispatch backend resolution. Fail-fast; never falls back to local."""

from typing import Literal

from maestro.execution.backend import ExecutionBackend
from maestro.execution.exec_config import (
    BackendSpec,
    DockerIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.local import LocalBackend


class ExecutionConfigError(Exception):
    """Raised for an unusable execution config (unknown/mis-specified backend)."""


class BackendResolver:
    """Resolves a backend name to an ExecutionBackend, caching instances."""

    def __init__(
        self,
        execution: ExecutionConfig | None,
        *,
        mode: Literal["scheduler", "orchestrator"] = "orchestrator",
    ) -> None:
        self._execution = execution or ExecutionConfig()
        self._registry = self._execution.normalized()
        self._mode = mode
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
        spec = self._registry.get(name)
        if spec is None:
            raise ExecutionConfigError(f"unknown backend {name!r}")
        transport = spec.transport
        if transport.type == "local":
            return self._build_local(name, spec)
        if isinstance(transport, SshTransport):
            if self._mode == "scheduler":
                raise ExecutionConfigError(
                    f"backend {name!r} uses ssh transport: SSH backends are "
                    "Mode-2 (orchestrator) only until Phase 2b"
                )
            return self._build_ssh(name, spec, transport)
        raise ExecutionConfigError(f"backend {name!r}: unsupported transport")

    def _build_local(self, name: str, spec: BackendSpec) -> ExecutionBackend:
        if isinstance(spec.isolation, DockerIsolation):
            from maestro.execution.docker_cli import DockerCli
            from maestro.execution.exec_config import DockerConfig
            from maestro.execution.isolators import DockerIsolator

            docker = DockerCli()
            docker_cfg = DockerConfig(
                image=spec.isolation.image,
                network=spec.isolation.network,
                memory=spec.isolation.memory,
                cpus=spec.isolation.cpus,
                user=spec.isolation.user,
                secret_env=spec.secret_env,
            )
            isolator = DockerIsolator(docker_cfg, docker=docker)
            return LocalBackend(isolator, backend_id=name, docker=docker)
        return LocalBackend(backend_id=name)

    def _build_ssh(self, name, spec, transport):  # replaced in Task D2
        raise ExecutionConfigError(
            f"backend {name!r}: ssh backend not yet wired (Task D2)"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backend_resolver_registry.py -v`
Expected: PASS (3).

- [ ] **Step 5: Regression — existing docker/local resolution still works**

Run: `uv run pytest tests/ -k "resolver or exec_config" -v`
Expected: PASS. Then:

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/resolver.py tests/test_backend_resolver_registry.py
git commit -m "feat(exec): resolver builds from registry; Mode-2-only SSH guard"
```

---

## Increment B — Shared infrastructure (secret-file, finalize contract, DB migration)

### Task 4 (B1): Extract the shared `secret_file` helper

**Files:**
- Create: `maestro/execution/secret_file.py`
- Modify: `maestro/execution/isolators.py:165-208` (`DockerIsolator.materialize`)
- Test: `tests/test_secret_file.py` (create)

**Interfaces:**
- Produces:
  - `validate_secret_value(name: str, value: str) -> None` — raises `ValueError` on `\n`/`\r`/`\x00`.
  - `write_env_file(path: Path, names: list[str], source_env: Mapping[str, str]) -> Path` — writes `KEY=value\n` lines for names present in `source_env`, file mode `0600` (dir assumed `0700`), validating each value.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_secret_file.py
import stat

import pytest

from maestro.execution.secret_file import validate_secret_value, write_env_file


def test_validate_rejects_control_chars():
    for bad in ("a\nb", "a\rb", "a\x00b"):
        with pytest.raises(ValueError, match="control char"):
            validate_secret_value("K", bad)


def test_write_env_file_is_0600_and_skips_absent(tmp_path):
    d = tmp_path / "sec"
    d.mkdir(mode=0o700)
    p = write_env_file(d / "env", ["A", "MISSING"], {"A": "x"})
    assert p.read_text() == "A=x\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_secret_file.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/secret_file.py
"""Shared 0600 env-file writer with control-char validation.

Extracted from DockerIsolator.materialize so the SSH backend reuses the exact
same secret-file discipline (values never in argv; forbidden control chars
rejected so a value cannot corrupt the KEY=value format or inject lines).
"""

import os
from collections.abc import Mapping
from pathlib import Path


def validate_secret_value(name: str, value: str) -> None:
    """Raise ValueError if a secret value has a forbidden control char."""
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError(f"secret {name} value has a forbidden control char")


def write_env_file(
    path: Path, names: list[str], source_env: Mapping[str, str]
) -> Path:
    """Write a 0600 env-file of `KEY=value` lines for names present in env."""
    lines: list[str] = []
    for name in names:
        if name not in source_env:
            continue
        value = source_env[name]
        validate_secret_value(name, value)
        lines.append(f"{name}={value}")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    path.chmod(0o600)
    return path
```

- [ ] **Step 4: Point `DockerIsolator.materialize` at the helper**

In `maestro/execution/isolators.py`, replace the inline env-file body (the `for key in plan.env_file_keys:` loop that builds `lines` and opens the fd) with:

```python
            if plan.env_file_keys:
                env_file = plan.tmp_dir / "env"
                write_env_file(env_file, plan.env_file_keys, os.environ)
```

Add `from maestro.execution.secret_file import write_env_file` at the top of `isolators.py`.

- [ ] **Step 5: Run tests to verify pass (helper + docker regression)**

Run: `uv run pytest tests/test_secret_file.py -v && uv run pytest tests/ -k "docker and (materialize or isolator or secret)" -v`
Expected: PASS.

- [ ] **Step 6: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/secret_file.py maestro/execution/isolators.py tests/test_secret_file.py
git commit -m "refactor(exec): extract shared secret_file helper from DockerIsolator"
```

---

### Task 5 (B2): Callback-driven `finalize_handle` (inter-phase persistence)

**Files:**
- Modify: `maestro/execution/finalize.py`
- Test: `tests/test_finalize_callbacks.py` (create)

**Interfaces:**
- Consumes: `TaskHandle` (`wait`/`collect`/`cleanup`).
- Produces:
  - `FinalizationResult(execution, collect_error=None, cleanup_error=None, collect_succeeded=False, cleanup_attempted=False)`, `.cleaned == cleanup_attempted and cleanup_error is None`.
  - `async finalize_handle(handle, *, on_terminal=None, on_collected=None) -> FinalizationResult` — awaits `wait()`, calls `on_terminal()`, then `collect()`; on collect failure returns early **without** cleanup; on success calls `on_collected()` then `cleanup()`.
  - `ensure_finalize_task(running, *, on_terminal=None, on_collected=None)` — single-owner task (idempotent), threading the callbacks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_finalize_callbacks.py
import anyio
import pytest

from maestro.execution.finalize import FinalizationResult, finalize_handle
from maestro.execution.models import CollectResult, ExecutionResult


class _Handle:
    def __init__(self, *, collect_raises=False):
        self.calls: list[str] = []
        self._collect_raises = collect_raises

    async def wait(self):
        self.calls.append("wait")
        return ExecutionResult(exit_code=0, output_log_path="/tmp/x")

    async def collect(self):
        self.calls.append("collect")
        if self._collect_raises:
            raise RuntimeError("conflict")
        return CollectResult(applied=True)

    async def cleanup(self):
        self.calls.append("cleanup")


@pytest.mark.anyio
async def test_collect_success_marks_between_phases_then_cleans():
    order: list[str] = []
    h = _Handle()
    fin = await finalize_handle(
        h,
        on_terminal=lambda: order.append("mark_terminal") or _noop(),
        on_collected=lambda: order.append("mark_collected") or _noop(),
    )
    assert h.calls == ["wait", "collect", "cleanup"]
    assert order == ["mark_terminal", "mark_collected"]
    assert fin.collect_succeeded and fin.cleaned


@pytest.mark.anyio
async def test_collect_failure_skips_cleanup_and_preserves():
    h = _Handle(collect_raises=True)
    fin = await finalize_handle(h, on_terminal=_acb(), on_collected=_acb())
    assert h.calls == ["wait", "collect"]  # NO cleanup
    assert not fin.collect_succeeded
    assert not fin.cleanup_attempted
    assert fin.collect_error == "conflict"


async def _noop():
    return None


def _acb():
    async def cb():
        return None

    return cb
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_finalize_callbacks.py -v`
Expected: FAIL — `finalize_handle` has no `on_terminal`/`on_collected`.

- [ ] **Step 3: Write minimal implementation**

Rewrite `maestro/execution/finalize.py`:

```python
"""Single-owner finalization: reap, then persist-phase → collect → persist-phase
→ cleanup. DB transitions happen BETWEEN phases (via callbacks), so a crash in
the collect→cleanup window can never leave durable state that lies.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from maestro.execution.backend import TaskHandle
from maestro.execution.models import ExecutionResult


_Callback = Callable[[], Awaitable[None]] | None


@dataclass
class FinalizationResult:
    execution: ExecutionResult
    collect_error: str | None = None
    cleanup_error: str | None = None
    collect_succeeded: bool = False
    cleanup_attempted: bool = False

    @property
    def cleaned(self) -> bool:
        return self.cleanup_attempted and self.cleanup_error is None


async def finalize_handle(
    handle: TaskHandle,
    *,
    on_terminal: _Callback = None,
    on_collected: _Callback = None,
) -> FinalizationResult:
    """Reap → persist terminal → collect → persist collected → cleanup."""
    execution = await handle.wait()
    if on_terminal is not None:
        await on_terminal()
    try:
        await handle.collect()
    except Exception as e:
        # Collect failed/conflicted: DO NOT clean up — resources are preserved.
        return FinalizationResult(execution, collect_error=str(e))
    if on_collected is not None:
        await on_collected()
    cleanup_error: str | None = None
    try:
        await handle.cleanup()
    except Exception as e:
        cleanup_error = str(e)
    return FinalizationResult(
        execution,
        cleanup_error=cleanup_error,
        collect_succeeded=True,
        cleanup_attempted=True,
    )


class _Finalizable(Protocol):
    handle: TaskHandle
    finalize_task: "asyncio.Task[FinalizationResult] | None"


def ensure_finalize_task(
    running: _Finalizable,
    *,
    on_terminal: _Callback = None,
    on_collected: _Callback = None,
) -> "asyncio.Task[FinalizationResult]":
    """Create the single finalization task for a running entity (idempotent)."""
    if running.finalize_task is None:
        running.finalize_task = asyncio.create_task(
            finalize_handle(
                running.handle, on_terminal=on_terminal, on_collected=on_collected
            )
        )
    return running.finalize_task
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_finalize_callbacks.py -v`
Expected: PASS (2).

- [ ] **Step 5: Regression — existing finalize callers still pass**

The Phase-1 call sites call `ensure_finalize_task(running)` with no callbacks — still valid (callbacks default `None`). Run:
Run: `uv run pytest tests/ -k "finalize or monitor" -v`
Expected: PASS.

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/finalize.py tests/test_finalize_callbacks.py
git commit -m "feat(exec): callback-driven finalize_handle with inter-phase persistence"
```

---

### Task 6 (B3): DB migration #8 — `collected` state + remote handle columns

**Files:**
- Modify: `maestro/database.py` (SCHEMA_SQL `execution_handles` at `:145`; migration registry near `:431`; add `_migrate_ssh_handle_columns`; `start_execution` at `:1251`; `mark_execution_state` at `:1343`; `get_open_execution_handles` at `:1392`)
- Test: `tests/test_db_migration_ssh_handles.py` (create)

**Interfaces:**
- Consumes: existing `execution_handles` table + `schema_migrations` journal.
- Produces:
  - `execution_handles.state` CHECK includes `'collected'`; new nullable columns `remote_host`, `remote_dir`, `status_marker`, `collected_at`.
  - `start_execution(..., remote_host=None, remote_dir=None, status_marker=None)` persists the three remote columns.
  - `mark_execution_state` accepts `new_state="collected"` (stamps `collected_at`, not `finished_at`).
  - `get_open_execution_handles()` selects `state IN ('prepared','running','terminal','collected')` and returns the new columns.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_migration_ssh_handles.py
import pytest

from maestro.database import Database


@pytest.mark.anyio
async def test_collected_state_and_remote_columns(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await db.start_execution(
        entity_kind="workstream",
        entity_id="api",
        expected_status="READY",
        running_status="RUNNING",
        execution_id="e1",
        backend_id="gpu",
        transport_ref='{"v":1,"transport":"ssh"}',
        attempt=1,
        remote_host="gpu",
        remote_dir="/var/tmp/maestro/maestro-exec-e1.ab",
        status_marker="/var/tmp/maestro/maestro-exec-e1.ab/e1.status",
    )
    await db.mark_execution_state("e1", "terminal", allowed_from=["prepared", "running"])
    await db.mark_execution_state("e1", "collected", allowed_from=["terminal"])
    rows = await db.get_open_execution_handles()
    row = next(r for r in rows if r["execution_id"] == "e1")
    assert row["state"] == "collected"
    assert row["remote_dir"].endswith("maestro-exec-e1.ab")
    assert row["status_marker"].endswith("e1.status")
    await db.close()
```

(Note: `start_execution` needs a workstream row `api` in `READY` first if it enforces the CAS; the test uses whatever minimal setup the existing `start_execution` tests use — mirror `tests/test_*execution*`/`test_database*` setup for a `READY` workstream before calling `start_execution`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db_migration_ssh_handles.py -v`
Expected: FAIL — `collected` violates CHECK / unknown columns.

- [ ] **Step 3: Update SCHEMA_SQL**

In `maestro/database.py`, change the `execution_handles` table (`:145`) `state` CHECK and add columns:

```sql
CREATE TABLE IF NOT EXISTS execution_handles (
    execution_id   TEXT PRIMARY KEY,
    entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
    entity_id      TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    backend_id     TEXT NOT NULL,
    transport_ref  TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','collected','cleaned')),
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    remote_host    TEXT,
    remote_dir     TEXT,
    status_marker  TEXT,
    collected_at   TEXT
);
```

- [ ] **Step 4: Add migration #8 (rebuild for CHECK change + ADD COLUMNs)**

Register `(8, "ssh_handle_columns", self._migrate_ssh_handle_columns)` in the migration list (`:431`), and add:

```python
    async def _migrate_ssh_handle_columns(self) -> None:
        """Add 'collected' state + remote columns to execution_handles.

        SQLite cannot alter a CHECK constraint in place, so rebuild the table
        (rename → create-new → copy → drop) and re-create its indexes. New
        columns are nullable, so existing rows copy cleanly.
        """
        assert self._connection is not None
        await self._connection.executescript(
            """
            ALTER TABLE execution_handles RENAME TO execution_handles_old;
            CREATE TABLE execution_handles (
                execution_id   TEXT PRIMARY KEY,
                entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
                entity_id      TEXT NOT NULL,
                attempt        INTEGER NOT NULL,
                backend_id     TEXT NOT NULL,
                transport_ref  TEXT NOT NULL,
                state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','collected','cleaned')),
                created_at     TEXT NOT NULL,
                finished_at    TEXT,
                remote_host    TEXT,
                remote_dir     TEXT,
                status_marker  TEXT,
                collected_at   TEXT
            );
            INSERT INTO execution_handles
                (execution_id, entity_kind, entity_id, attempt, backend_id,
                 transport_ref, state, created_at, finished_at)
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at
            FROM execution_handles_old;
            DROP TABLE execution_handles_old;
            CREATE INDEX IF NOT EXISTS ix_exec_state_backend
                ON execution_handles (state, backend_id);
            CREATE INDEX IF NOT EXISTS ix_exec_entity
                ON execution_handles (entity_kind, entity_id, attempt);
            """
        )
        await self._connection.commit()
```

- [ ] **Step 5: Extend `start_execution`, `mark_execution_state`, `get_open_execution_handles`**

`start_execution`: add kwargs `remote_host=None, remote_dir=None, status_marker=None` and include them in the `INSERT INTO execution_handles (...)` column list + values.

`mark_execution_state`: stamp `collected_at` when `new_state == "collected"`:

```python
        collected_at = (
            _format_datetime(datetime.now(UTC)) if new_state == "collected" else None
        )
        finished_at = (
            _format_datetime(datetime.now(UTC))
            if new_state in ("terminal", "cleaned")
            else None
        )
        await self._connection.execute(
            f"""
            UPDATE execution_handles
            SET state = ?,
                finished_at = COALESCE(?, finished_at),
                collected_at = COALESCE(?, collected_at)
            WHERE execution_id = ? AND state IN ({placeholders})
            """,
            (new_state, finished_at, collected_at, execution_id, *allowed_from),
        )
```

`get_open_execution_handles`: widen the `WHERE state IN (...)` to include `'collected'` and add the new columns to the `SELECT`:

```python
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at,
                   remote_host, remote_dir, status_marker, collected_at
            FROM execution_handles
            WHERE state IN ('prepared', 'running', 'terminal', 'collected')
              AND backend_id != 'local'
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_db_migration_ssh_handles.py -v`
Expected: PASS.

- [ ] **Step 7: Regression — existing DB + docker recovery tests**

Run: `uv run pytest tests/ -k "database or migration or execution_handle or docker_recovery" -v`
Expected: PASS (docker path unaffected: `collected` is an added, not required, mark).

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/database.py tests/test_db_migration_ssh_handles.py
git commit -m "feat(db): migration #8 — collected state + remote handle columns"
```

---

## Increment C — SSH transport primitives (CLI, launch builders, supervisor)

### Task 7 (C1): `SshCli` — guarded ssh/rsync argv with an injectable runner

**Files:**
- Create: `maestro/execution/ssh_cli.py`
- Test: `tests/test_ssh_cli.py` (create)

**Interfaces:**
- Consumes: `SshTransport` (A1).
- Produces:
  - `RunResult(returncode: int, stdout: str, stderr: str)`
  - `Runner = Callable[[list[str], str | None], Awaitable[RunResult]]` (argv, stdin) — injected; the default runs `asyncio.create_subprocess_exec`.
  - `SshCli(transport: SshTransport, *, runner: Runner | None = None)` with:
    - `ssh_base() -> list[str]` — `["ssh", *guarded_opts, *user_flag, *port_flag, host]` where guarded opts (BatchMode=yes, StrictHostKeyChecking=yes, ConnectTimeout, PasswordAuthentication=no, KbdInteractiveAuthentication=no) come **after** whitelisted `ssh_opts` so they win.
    - `validate_ssh_opts()` — raises `ValueError` if any `ssh_opt` sets a guarded key.
    - `async run(argv: list[str], *, stdin: str | None = None) -> RunResult` — runs `ssh_base() + argv` via the runner.
    - `async check(argv: list[str]) -> bool` — `run(...).returncode == 0`.
    - `rsync_argv(src: str, dst: str, *, delete: bool, excludes: list[str]) -> list[str]` — `["rsync","-a",("--delete"?),*("--exclude",e...),"-e",<ssh -e string with guarded opts>,src,dst]`.
    - `async probe_tool(tool: str) -> bool` — validates `tool` matches `^[A-Za-z0-9._-]+$` (else `ValueError`) then `check(["command","-v","--",tool])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_cli.py
import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli


def _t(**kw):
    return SshTransport(type="ssh", host="gpu", workdir_root="/w", **kw)


def test_guarded_opts_present_and_last():
    base = SshCli(_t(ssh_opts=["-o", "ServerAliveInterval=15"])).ssh_base()
    assert base[0] == "ssh"
    assert base[-1] == "gpu"
    joined = " ".join(base)
    for guard in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ConnectTimeout=10",
        "PasswordAuthentication=no",
    ):
        assert guard in joined
    # user opt appears BEFORE the guarded block
    assert joined.index("ServerAliveInterval=15") < joined.index("BatchMode=yes")


def test_user_and_port_are_flags_not_host_token():
    base = SshCli(_t(user="alice", port=2222)).ssh_base()
    assert "-l" in base and "alice" in base
    assert "-p" in base and "2222" in base
    assert base[-1] == "gpu"


def test_ssh_opts_cannot_override_guarded_key():
    with pytest.raises(ValueError, match="guarded"):
        SshCli(_t(ssh_opts=["-o", "StrictHostKeyChecking=no"])).validate_ssh_opts()


def test_probe_tool_rejects_bad_name():
    with pytest.raises(ValueError, match="tool name"):
        import anyio

        anyio.run(SshCli(_t()).probe_tool, "spec runner; rm -rf /")


@pytest.mark.anyio
async def test_run_uses_injected_runner():
    seen = {}

    async def fake(argv, stdin):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return RunResult(0, "ok", "")

    cli = SshCli(_t(), runner=fake)
    res = await cli.run(["true"], stdin="x")
    assert res.returncode == 0 and seen["stdin"] == "x"
    assert seen["argv"][0] == "ssh" and seen["argv"][-2:] == ["gpu", "true"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_cli.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_cli.py
"""Guarded ssh/rsync argv builder over an injectable command runner.

The runner injection makes the whole SSH backend unit-testable with no real
sshd. Maestro's security options are appended AFTER any whitelisted user
`ssh_opts`, so a user option can never disable BatchMode / host-key
verification / connect timeout / password-auth-off.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from maestro.execution.exec_config import SshTransport


_TOOL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GUARDED_KEYS = {
    "BatchMode",
    "StrictHostKeyChecking",
    "ConnectTimeout",
    "PasswordAuthentication",
    "KbdInteractiveAuthentication",
}


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], str | None], Awaitable[RunResult]]


async def _default_runner(argv: list[str], stdin: str | None) -> RunResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    return RunResult(
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class SshCli:
    def __init__(self, transport: SshTransport, *, runner: Runner | None = None) -> None:
        self._t = transport
        self._runner = runner or _default_runner

    @property
    def host(self) -> str:
        return self._t.host

    @property
    def workdir_root(self) -> str:
        return self._t.workdir_root

    def validate_ssh_opts(self) -> None:
        """Reject any user ssh_opt that sets a guarded key."""
        opts = self._t.ssh_opts
        for i, tok in enumerate(opts):
            if tok == "-o" and i + 1 < len(opts):
                key = opts[i + 1].split("=", 1)[0].strip()
                if key in _GUARDED_KEYS:
                    raise ValueError(f"ssh_opt sets guarded key {key!r}")

    def _guarded_opts(self) -> list[str]:
        return [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"ConnectTimeout={self._t.connect_timeout_s}",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
        ]

    def _endpoint_flags(self) -> list[str]:
        flags: list[str] = []
        if self._t.user:
            flags += ["-l", self._t.user]
        if self._t.port:
            flags += ["-p", str(self._t.port)]
        return flags

    def ssh_base(self) -> list[str]:
        self.validate_ssh_opts()
        return [
            "ssh",
            *self._t.ssh_opts,
            *self._guarded_opts(),
            *self._endpoint_flags(),
            self._t.host,
        ]

    def _rsync_ssh_string(self) -> str:
        parts = ["ssh", *self._t.ssh_opts, *self._guarded_opts()]
        if self._t.port:
            parts += ["-p", str(self._t.port)]
        return " ".join(parts)

    def rsync_argv(
        self, src: str, dst: str, *, delete: bool, excludes: list[str]
    ) -> list[str]:
        self.validate_ssh_opts()
        argv = ["rsync", "-a"]
        if delete:
            argv.append("--delete")
        for exc in excludes:
            argv += ["--exclude", exc]
        argv += ["-e", self._rsync_ssh_string(), src, dst]
        return argv

    async def run(self, argv: list[str], *, stdin: str | None = None) -> RunResult:
        return await self._runner(self.ssh_base() + argv, stdin)

    async def rsync(
        self, src: str, dst: str, *, delete: bool, excludes: list[str]
    ) -> RunResult:
        """Run an rsync (over the guarded ssh transport) via the same runner."""
        return await self._runner(
            self.rsync_argv(src, dst, delete=delete, excludes=excludes), None
        )

    async def check(self, argv: list[str]) -> bool:
        return (await self.run(argv)).returncode == 0

    async def probe_tool(self, tool: str) -> bool:
        if not _TOOL_RE.match(tool):
            raise ValueError(f"invalid tool name {tool!r}")
        return await self.check(["command", "-v", "--", tool])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_cli.py -v`
Expected: PASS (5).

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_cli.py tests/test_ssh_cli.py
git commit -m "feat(exec): SshCli guarded argv builder over injectable runner"
```

---

### Task 8 (C2): Launch-descriptor + remote-layout builders (pure)

**Files:**
- Create: `maestro/execution/ssh_launch.py`
- Test: `tests/test_ssh_launch.py` (create)

**Interfaces:**
- Produces:
  - `RemoteLayout(root: str, repo: str, env_file: str, descriptor: str, supervisor: str, owner_marker: str, pid: str, status: str, log: str)` — all absolute remote paths under `<tmp>`.
  - `remote_layout(workdir_root: str, execution_id: str) -> RemoteLayout` — `<workdir_root>/maestro-exec-<execution_id>` and its fixed children.
  - `build_descriptor(execution_id, layout, argv, workdir_root) -> dict` — the JSON descriptor (`{"v":1,"execution_id","cwd":layout.repo,"argv","env_file","workdir_root","owner_marker","pid_file","status_file","log_file"}`).
  - `RSYNC_EXCLUDES_OUT: list[str]` (`[".git",".maestro","*.log"]`) and `RSYNC_EXCLUDES_COLLECT: list[str]` (`[".git",".maestro","*.log","env",".maestro-owner","*.status","*.pid"]`).
  - `encode_transport_ref(host, port, remote_dir, status_marker) -> str` — opaque versioned JSON string.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_launch.py
import json

from maestro.execution.ssh_launch import (
    build_descriptor,
    encode_transport_ref,
    remote_layout,
)


def test_remote_layout_paths_are_under_tmp():
    lay = remote_layout("/var/tmp/maestro", "e1")
    assert lay.root == "/var/tmp/maestro/maestro-exec-e1"
    assert lay.repo == "/var/tmp/maestro/maestro-exec-e1/repo"
    assert lay.status.endswith("/e1.status")
    assert lay.owner_marker.endswith("/.maestro-owner")


def test_descriptor_carries_argv_verbatim():
    lay = remote_layout("/w", "e1")
    d = build_descriptor("e1", lay, ["spec-runner", "run", "--all"], "/w")
    assert d["v"] == 1
    assert d["argv"] == ["spec-runner", "run", "--all"]
    assert d["cwd"] == lay.repo
    # round-trips as JSON
    assert json.loads(json.dumps(d))["execution_id"] == "e1"


def test_transport_ref_is_opaque_versioned_json():
    ref = encode_transport_ref("gpu", 2222, "/w/maestro-exec-e1", "/w/maestro-exec-e1/e1.status")
    obj = json.loads(ref)
    assert obj["v"] == 1 and obj["transport"] == "ssh" and obj["host"] == "gpu"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_launch.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_launch.py
"""Pure builders for the SSH launch: remote path layout, JSON descriptor,
rsync exclude sets, opaque transport_ref. No I/O — trivially unit-testable.
"""

import json
from dataclasses import dataclass

RSYNC_EXCLUDES_OUT = [".git", ".maestro", "*.log"]
RSYNC_EXCLUDES_COLLECT = [
    ".git",
    ".maestro",
    "*.log",
    "env",
    ".maestro-owner",
    "*.status",
    "*.pid",
    "repo/.git",
]


@dataclass(frozen=True)
class RemoteLayout:
    root: str
    repo: str
    env_file: str
    descriptor: str
    supervisor: str
    owner_marker: str
    pid: str
    status: str
    log: str


def remote_layout(workdir_root: str, execution_id: str) -> RemoteLayout:
    root = f"{workdir_root.rstrip('/')}/maestro-exec-{execution_id}"
    return RemoteLayout(
        root=root,
        repo=f"{root}/repo",
        env_file=f"{root}/env",
        descriptor=f"{root}/descriptor.json",
        supervisor=f"{root}/maestro_supervisor.py",
        owner_marker=f"{root}/.maestro-owner",
        pid=f"{root}/{execution_id}.pid",
        status=f"{root}/{execution_id}.status",
        log=f"{root}/{execution_id}.log",
    )


def build_descriptor(
    execution_id: str,
    layout: RemoteLayout,
    argv: list[str],
    workdir_root: str,
) -> dict:
    return {
        "v": 1,
        "execution_id": execution_id,
        "cwd": layout.repo,
        "argv": list(argv),
        "env_file": layout.env_file,
        "workdir_root": workdir_root,
        "owner_marker": layout.owner_marker,
        "pid_file": layout.pid,
        "status_file": layout.status,
        "log_file": layout.log,
    }


def encode_transport_ref(
    host: str, port: int | None, remote_dir: str, status_marker: str
) -> str:
    return json.dumps(
        {
            "v": 1,
            "transport": "ssh",
            "host": host,
            "port": port,
            "remote_dir": remote_dir,
            "status_marker": status_marker,
        }
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_launch.py -v`
Expected: PASS (3).

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_launch.py tests/test_ssh_launch.py
git commit -m "feat(exec): pure SSH launch builders (layout, descriptor, transport_ref)"
```

---

### Task 9 (C3): The remote Python supervisor (daemonizing, atomic status)

**Files:**
- Create: `maestro/execution/resources/__init__.py` (empty), `maestro/execution/resources/maestro_supervisor.py`
- Modify: `pyproject.toml` (package-data / include the resource)
- Test: `tests/test_supervisor_local.py` (create) — runs the supervisor as a subprocess **locally** (no SSH) against a temp dir.

**Interfaces:**
- Produces a standalone script `maestro_supervisor.py <descriptor.json>` that:
  - reads the descriptor; validates `owner_marker`/paths are under `workdir_root`; writes the owner marker (`execution_id`);
  - **daemonizes**: `fork()`; parent prints a one-line handshake `MAESTRO-SUPERVISOR-READY <execution_id>` to stdout and exits `0`; child `os.setsid()`, redirects stdin←`/dev/null`, workload stdout/stderr → `log_file`;
  - loads `env_file` (if present) into `os.environ`-derived child env;
  - `subprocess.Popen(argv, cwd, env, start_new_session=True)`; writes `pid_file` (`{"pid":..,"pgid":..}`);
  - waits; writes `status_file` atomically (`tmp` → `flush`/`os.fsync` → `os.replace`) as `{"pid","pgid","exit_code","completed_at"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_supervisor_local.py
import json
import subprocess
import sys
import time
from pathlib import Path

SUP = Path("maestro/execution/resources/maestro_supervisor.py").resolve()


def _descriptor(tmp: Path, argv: list[str]) -> Path:
    root = tmp / "maestro-exec-e1"
    (root / "repo").mkdir(parents=True)
    d = {
        "v": 1,
        "execution_id": "e1",
        "cwd": str(root / "repo"),
        "argv": argv,
        "env_file": str(root / "env"),
        "workdir_root": str(tmp),
        "owner_marker": str(root / ".maestro-owner"),
        "pid_file": str(root / "e1.pid"),
        "status_file": str(root / "e1.status"),
        "log_file": str(root / "e1.log"),
    }
    dp = root / "descriptor.json"
    dp.write_text(json.dumps(d))
    return dp


def _wait_status(path: Path, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError("status marker never appeared")


def test_supervisor_handshake_and_atomic_status(tmp_path):
    dp = _descriptor(tmp_path, [sys.executable, "-c", "print('hi')"])
    # Launch returns quickly after the handshake (parent exits post-fork).
    out = subprocess.run(
        [sys.executable, str(SUP), str(dp)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0
    assert out.stdout.strip().startswith("MAESTRO-SUPERVISOR-READY e1")
    status = _wait_status(tmp_path / "maestro-exec-e1" / "e1.status")
    assert status["exit_code"] == 0
    assert (tmp_path / "maestro-exec-e1" / ".maestro-owner").read_text().strip() == "e1"


def test_supervisor_preserves_argv_boundaries(tmp_path):
    marker = tmp_path / "argmark.txt"
    argv = [sys.executable, "-c", "import sys; open(sys.argv[1],'w').write(sys.argv[2])",
            str(marker), "a b\t\"c\" $(x)"]
    dp = _descriptor(tmp_path, argv)
    subprocess.run([sys.executable, str(SUP), str(dp)], timeout=10)
    _wait_status(tmp_path / "maestro-exec-e1" / "e1.status")
    assert marker.read_text() == 'a b\t"c" $(x)'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_supervisor_local.py -v`
Expected: FAIL — supervisor file missing.

- [ ] **Step 3: Write the supervisor**

```python
# maestro/execution/resources/maestro_supervisor.py
"""Fixed, versioned Maestro remote supervisor. Stdlib only.

Launched as `python3 maestro_supervisor.py <descriptor.json>`. Daemonizes so it
outlives the launch SSH channel, owns the workload process group, and writes an
atomic status marker the center's monitor polls. NO dynamic argv is ever
interpolated into this source — everything comes from the descriptor.
"""

import json
import os
import subprocess
import sys
import time

SUPERVISOR_VERSION = 1
HANDSHAKE = "MAESTRO-SUPERVISOR-READY"


def _fail(msg: str) -> None:
    sys.stderr.write(f"supervisor: {msg}\n")
    sys.stderr.flush()
    os._exit(2)


def _validate(desc: dict) -> None:
    root = f"{desc['workdir_root'].rstrip('/')}/maestro-exec-{desc['execution_id']}"
    for key in ("cwd", "owner_marker", "pid_file", "status_file", "log_file"):
        if not str(desc[key]).startswith(root + "/") and desc[key] != root:
            _fail(f"path {key} escapes {root}")


def _atomic_write(path: str, data: str) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_env(env_file: str) -> dict:
    env = dict(os.environ)
    if os.path.exists(env_file):
        with open(env_file) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key] = value
    return env


def _run_workload(desc: dict) -> None:
    # Detached child: no controlling terminal, own session/process group.
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    log_fd = os.open(desc["log_file"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)

    env = _load_env(desc["env_file"])
    proc = subprocess.Popen(
        desc["argv"],
        cwd=desc["cwd"],
        env=env,
        start_new_session=True,
    )
    _atomic_write(desc["pid_file"], json.dumps({"pid": proc.pid, "pgid": proc.pid}))
    exit_code = proc.wait()
    _atomic_write(
        desc["status_file"],
        json.dumps(
            {
                "pid": proc.pid,
                "pgid": proc.pid,
                "exit_code": exit_code,
                "completed_at": time.time(),
            }
        ),
    )
    os._exit(0)


def main() -> None:
    if len(sys.argv) != 2:
        _fail("usage: maestro_supervisor.py <descriptor.json>")
    with open(sys.argv[1]) as fh:
        desc = json.load(fh)
    _validate(desc)
    with open(desc["owner_marker"], "w") as fh:
        fh.write(desc["execution_id"] + "\n")

    pid = os.fork()
    if pid > 0:
        # Parent: confirm start, emit handshake, end the launch SSH command.
        sys.stdout.write(f"{HANDSHAKE} {desc['execution_id']}\n")
        sys.stdout.flush()
        os._exit(0)
    # Child: daemonized supervisor.
    _run_workload(desc)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Ship the resource as package data**

In `pyproject.toml`, ensure the resource ships (under `[tool.hatch.build]` / `[tool.setuptools.package-data]` per the project's build backend). Add a glob for `maestro/execution/resources/*.py`. Verify the project's existing `maestro/resources/` packaging pattern (Phase-1 catalog templates) and mirror it exactly.

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_supervisor_local.py -v`
Expected: PASS (2) — handshake emitted, atomic status written, argv boundaries preserved.

- [ ] **Step 6: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/resources/ tests/test_supervisor_local.py pyproject.toml
git commit -m "feat(exec): daemonizing remote supervisor with atomic status marker"
```

---

## Increment D — Collect (transactional) + progress mirror

### Task 10 (D1): Baseline + two-phase transactional collect

**Files:**
- Create: `maestro/execution/ssh_collect.py`
- Test: `tests/test_ssh_collect.py` (create)

**Interfaces:**
- Produces:
  - `class CollectConflict(Exception)` — preflight rejected (no worktree mutation happened).
  - `capture_baseline(worktree: Path, *, excludes: list[str]) -> dict[str, str]` — `{relpath: sha256}` over all files not matching `excludes`.
  - `plan_collect(worktree, staging, baseline, *, forbidden: list[str]) -> CollectPlan` — computes `modified`/`deleted` (remote-vs-baseline), validates conflicts (local-vs-baseline on remote-touched paths), forbidden paths, symlink/traversal escapes. Raises `CollectConflict` on any violation — **no side effects**.
  - `CollectPlan(modified: list[str], deleted: list[str])`.
  - `apply_collect(worktree, staging, plan, *, journal_dir: Path) -> None` — backs up affected paths into `journal_dir`, applies atomically per file; on any error restores all backups and re-raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_collect.py
import hashlib
from pathlib import Path

import pytest

from maestro.execution.ssh_collect import (
    CollectConflict,
    apply_collect,
    capture_baseline,
    plan_collect,
)

EXCL = [".git", ".maestro", "*.log"]
FORBIDDEN = [".git", ".maestro"]


def _w(p: Path, name: str, body: str) -> None:
    (p / name).parent.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(body)


def test_modified_new_and_deleted_detected(tmp_path):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir(); st.mkdir()
    _w(wt, "a.py", "orig"); _w(wt, "gone.py", "x")
    base = capture_baseline(wt, excludes=EXCL)
    _w(st, "a.py", "changed"); _w(st, "new.py", "n")  # gone.py absent -> deleted
    plan = plan_collect(wt, st, base, forbidden=FORBIDDEN)
    assert set(plan.modified) == {"a.py", "new.py"}
    assert plan.deleted == ["gone.py"]
    apply_collect(wt, st, plan, journal_dir=tmp_path / "j")
    assert (wt / "a.py").read_text() == "changed"
    assert (wt / "new.py").read_text() == "n"
    assert not (wt / "gone.py").exists()


def test_local_divergence_on_remote_touched_path_conflicts(tmp_path):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir(); st.mkdir()
    _w(wt, "a.py", "orig")
    base = capture_baseline(wt, excludes=EXCL)
    _w(wt, "a.py", "LOCALLY CHANGED DURING RUN")  # parallel local mutation
    _w(st, "a.py", "remote changed")
    with pytest.raises(CollectConflict):
        plan_collect(wt, st, base, forbidden=FORBIDDEN)


def test_preflight_conflict_leaves_worktree_untouched(tmp_path):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir(); st.mkdir()
    _w(wt, "a.py", "orig"); _w(st, "a.py", "orig")
    _w(st, "../escape.py", "evil")  # traversal
    base = capture_baseline(wt, excludes=EXCL)
    before = (wt / "a.py").read_text()
    with pytest.raises(CollectConflict):
        plan_collect(wt, st, base, forbidden=FORBIDDEN)
    assert (wt / "a.py").read_text() == before


def test_rollback_restores_on_apply_error(tmp_path, monkeypatch):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir(); st.mkdir()
    _w(wt, "a.py", "orig"); _w(wt, "b.py", "orig-b")
    base = capture_baseline(wt, excludes=EXCL)
    _w(st, "a.py", "A2"); _w(st, "b.py", "B2")
    plan = plan_collect(wt, st, base, forbidden=FORBIDDEN)
    # Force a failure after the first file is applied.
    import maestro.execution.ssh_collect as mod
    calls = {"n": 0}
    real = mod._atomic_copy
    def boom(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(mod, "_atomic_copy", boom)
    with pytest.raises(OSError):
        apply_collect(wt, st, plan, journal_dir=tmp_path / "j")
    assert (wt / "a.py").read_text() == "orig"  # rolled back
    assert (wt / "b.py").read_text() == "orig-b"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_collect.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_collect.py
"""Baseline capture + two-phase transactional collect.

Phase 1 (plan_collect): pure preflight — zero worktree mutation. Detects the
remote's changes vs a pre-run baseline, rejects conflicts (parallel local
mutation on a remote-touched path), forbidden paths and symlink/traversal
escapes. Phase 2 (apply_collect): back up affected paths into a journal, apply
atomically per file, and on any error restore the whole journal.
"""

import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class CollectConflict(Exception):
    """Preflight rejected the collect; no worktree changes were made."""


@dataclass
class CollectPlan:
    modified: list[str]
    deleted: list[str]


def _excluded(rel: str, excludes: list[str]) -> bool:
    parts = rel.split("/")
    for pat in excludes:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path, excludes: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(root).as_posix()
            if _excluded(rel, excludes):
                continue
            if abs_p.is_symlink():
                continue  # symlinks handled/validated in plan_collect
            out[rel] = _sha(abs_p)
    return out


def capture_baseline(worktree: Path, *, excludes: list[str]) -> dict[str, str]:
    """{relpath: sha256} for all non-excluded regular files in the worktree."""
    return _walk(worktree, excludes)


def _rel_escapes(worktree: Path, rel: str) -> bool:
    target = (worktree / rel).resolve()
    root = worktree.resolve()
    return root != target and root not in target.parents


def plan_collect(
    worktree: Path,
    staging: Path,
    baseline: dict[str, str],
    *,
    forbidden: list[str],
) -> CollectPlan:
    """Preflight; raises CollectConflict on any violation. No side effects."""
    remote = _walk(staging, forbidden)
    # Symlink / traversal guard over the raw staging tree.
    for dirpath, _dirs, files in os.walk(staging):
        for name in files:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(staging).as_posix()
            if abs_p.is_symlink():
                raise CollectConflict(f"symlink in staging rejected: {rel}")
            if ".." in rel.split("/") or _rel_escapes(worktree, rel):
                raise CollectConflict(f"path escapes worktree: {rel}")

    modified = sorted(r for r, sha in remote.items() if baseline.get(r) != sha)
    deleted = sorted(r for r in baseline if r not in remote)

    for rel in [*modified, *deleted]:
        if _excluded(rel, forbidden):
            raise CollectConflict(f"forbidden path in change set: {rel}")
        current_p = worktree / rel
        current = _sha(current_p) if current_p.is_file() else None
        if current != baseline.get(rel):
            raise CollectConflict(
                f"local worktree diverged from baseline on remote-touched path: {rel}"
            )
    return CollectPlan(modified=modified, deleted=deleted)


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.maestro-tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def apply_collect(
    worktree: Path,
    staging: Path,
    plan: CollectPlan,
    *,
    journal_dir: Path,
) -> None:
    """Apply with a rollback journal; restore everything on any error."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    backed: list[tuple[str, Path | None]] = []  # (rel, backup_path or None if absent)
    try:
        for rel in [*plan.modified, *plan.deleted]:
            target = worktree / rel
            if target.is_file():
                bak = journal_dir / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, bak)
                backed.append((rel, bak))
            else:
                backed.append((rel, None))
        for rel in plan.modified:
            _atomic_copy(staging / rel, worktree / rel)
        for rel in plan.deleted:
            (worktree / rel).unlink(missing_ok=True)
    except Exception:
        for rel, bak in backed:
            target = worktree / rel
            if bak is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bak, target)
        raise
    shutil.rmtree(journal_dir, ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_collect.py -v`
Expected: PASS (4).

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_collect.py tests/test_ssh_collect.py
git commit -m "feat(exec): two-phase transactional collect with rollback journal"
```

---

### Task 11 (D2): WAL-safe progress mirror (`sqlite3.backup()` snapshot)

**Files:**
- Create: `maestro/execution/ssh_mirror.py`
- Test: `tests/test_ssh_mirror.py` (create)

**Interfaces:**
- Produces:
  - `SNAPSHOT_SCRIPT: str` — a stdlib Python snippet piped to `python3 - <src_db> <dst_snapshot>` that opens `src` read-only, runs `sqlite3.Connection.backup()` into a temp file, and `os.replace`s it to `dst`. Source paths arrive as `sys.argv`, never interpolated.
  - `async mirror_once(ssh: SshCli, remote_db: str, remote_snapshot: str, local_target: Path) -> bool` — remote snapshot, rsync single file, atomic-replace into `local_target`. Returns True on success, False on a transient failure (logged, not raised).
  - `snapshot_locally(src_db: Path, dst: Path) -> None` — the same backup used by tests (and by a localhost run), so the snapshot logic is exercised without SSH.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_mirror.py
import sqlite3

from maestro.execution.ssh_mirror import snapshot_locally


def test_snapshot_is_consistent_under_active_writer(tmp_path):
    src = tmp_path / "live.db"
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    con.commit()
    # Keep a writer open (uncommitted) to prove backup() is consistent.
    con.execute("INSERT INTO t VALUES (999)")
    snap = tmp_path / "snap.db"
    snapshot_locally(src, snap)
    rcon = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    (count,) = rcon.execute("SELECT count(*) FROM t").fetchone()
    assert count == 100  # committed rows only; no DatabaseError
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_mirror.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_mirror.py
"""WAL-safe progress mirror: a remote sqlite3.backup() snapshot of the live DB,
transferred as a single file. Sequential rsync of .db/.db-wal/.db-shm is NOT a
valid snapshot protocol (see spec §F); a consistent backup() is.
"""

import os
import shutil
import sqlite3
from pathlib import Path

from maestro.execution.ssh_cli import SshCli


SNAPSHOT_SCRIPT = """\
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
tmp = dst + ".tmp"
source = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
target = sqlite3.connect(tmp)
with target:
    source.backup(target)
target.close(); source.close()
os.replace(tmp, dst)
"""


def snapshot_locally(src_db: Path, dst: Path) -> None:
    """Consistent snapshot of a live (WAL) DB into dst (used by tests/localhost)."""
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    source = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    target = sqlite3.connect(str(tmp))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()
    os.replace(tmp, dst)


async def mirror_once(
    ssh: SshCli, remote_db: str, remote_snapshot: str, local_target: Path
) -> bool:
    """One mirror tick: remote snapshot → rsync one file → atomic local replace."""
    res = await ssh.run(
        ["python3", "-", remote_db, remote_snapshot], stdin=SNAPSHOT_SCRIPT
    )
    if res.returncode != 0:
        return False
    tmp = local_target.with_suffix(local_target.suffix + ".tmp")
    pulled = await ssh.rsync(
        f"{ssh.host}:{remote_snapshot}",  # host embedded per rsync convention
        str(tmp),
        delete=False,
        excludes=[],
    )
    if pulled.returncode != 0:
        return False
    if tmp.exists():
        shutil.move(str(tmp), str(local_target))
    return True
```

(Implementer note: `mirror_once` uses the public `ssh.rsync(...)` (same injected runner), so it is fake-able; the `ssh.host` embedding in the rsync source is the one place a host token is concatenated, and it is Maestro-controlled (validated bare host from A1), never user argv.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_mirror.py -v`
Expected: PASS.

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_mirror.py tests/test_ssh_mirror.py
git commit -m "feat(exec): WAL-safe progress mirror via remote sqlite3.backup snapshot"
```

---

## Increment E — SshTaskHandle, SshBackend, recovery

### Task 12 (E1): `SshTaskHandle` + monitor (cached poll, status marker, pgroup signals)

**Files:**
- Create: `maestro/execution/ssh_handle.py`
- Test: `tests/test_ssh_handle.py` (create)

**Interfaces:**
- Consumes: `SshCli` (C1), `RemoteLayout`/`RSYNC_EXCLUDES_COLLECT` (C2), `ssh_collect` (D1), `ExecutionHandleRef`/`ExecutionResult`/`CollectResult` (models).
- Produces:
  - `CollectSpec(worktree: Path, staging_dir: Path, journal_dir: Path, baseline: dict[str, str])`
  - `SshTaskHandle(ssh, layout, ref, *, log_path, timeout_seconds, collect_spec, poll_interval=1.0)` implementing the `TaskHandle` protocol.
  - `start(self) -> None` — spawns the monitor task; returns after nothing (the backend calls it and awaits the handshake separately, Task E2).
  - `os_pid` → `None`; `poll()` → cached exit code; `wait()` awaits terminal; `terminate`/`kill` signal the pgroup; `collect()` = rsync→plan→apply (raises `CollectConflict`); `cleanup()` = guarded remote `rm -rf` + local staging/journal removal.

- [ ] **Step 1: Write the failing test** (fake-runner driven; no sshd)

```python
# tests/test_ssh_handle.py
import json
from pathlib import Path

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_handle import CollectSpec, SshTaskHandle
from maestro.execution.ssh_launch import remote_layout


def _ref() -> ExecutionHandleRef:
    from datetime import UTC, datetime

    return ExecutionHandleRef(
        backend_id="gpu", run_id="api", transport_ref="{}", started_at=datetime.now(UTC)
    )


class FakeSsh:
    """Scripts responses by matching a substring in the joined remote argv."""

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.calls.append(argv)
        for needle, result in self._responses:
            if needle in " ".join(argv):
                return result
        return RunResult(0, "", "")


@pytest.mark.anyio
async def test_poll_is_cached_and_wait_reads_status(tmp_path, monkeypatch):
    status = json.dumps({"pid": 5, "pgid": 5, "exit_code": 0, "completed_at": 1.0})
    fake = FakeSsh([("cat", RunResult(0, status, ""))])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    h = SshTaskHandle(
        ssh, layout, _ref(),
        log_path=tmp_path / "log", timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    h.start()
    res = await h.wait()
    assert res.exit_code == 0
    assert h.poll() == 0  # cached, no I/O


@pytest.mark.anyio
async def test_terminate_signals_process_group(tmp_path):
    status = json.dumps({"pid": 5, "pgid": 42, "exit_code": 143, "completed_at": 1.0})
    pidf = json.dumps({"pid": 5, "pgid": 42})
    fake = FakeSsh([("cat", RunResult(0, status, "")), (".pid", RunResult(0, pidf, ""))])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = SshTaskHandle(
        ssh, remote_layout("/w", "e1"), _ref(),
        log_path=tmp_path / "log", timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    await h.terminate(0.01)
    kill_calls = [c for c in fake.calls if "kill" in " ".join(c)]
    assert any("-42" in " ".join(c) for c in kill_calls)  # negative pgid = group


@pytest.mark.anyio
async def test_cleanup_refuses_when_owner_marker_mismatch(tmp_path):
    # owner marker cat returns a DIFFERENT execution_id → refuse rm -rf
    fake = FakeSsh([(".maestro-owner", RunResult(0, "OTHER\n", ""))])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    h = SshTaskHandle(
        ssh, layout, _ref(),
        log_path=tmp_path / "log", timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
        expected_owner="e1",
    )
    with pytest.raises(RuntimeError, match="owner"):
        await h.cleanup()
    assert not any("rm -rf" in " ".join(c) for c in fake.calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_handle.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_handle.py
"""SshTaskHandle + local monitor. poll() is cached-only (never network I/O);
the monitor tails the remote log at a byte offset and polls the atomic status
marker. Signals target the workload's process GROUP (negative pgid).
"""

import asyncio
import contextlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)
from maestro.execution.ssh_cli import SshCli
from maestro.execution.ssh_collect import (
    apply_collect,
    capture_baseline,
    plan_collect,
)
from maestro.execution.ssh_launch import RSYNC_EXCLUDES_COLLECT, RemoteLayout


@dataclass
class CollectSpec:
    worktree: Path
    staging_dir: Path
    journal_dir: Path
    baseline: dict[str, str]


class SshTaskHandle:
    def __init__(
        self,
        ssh: SshCli,
        layout: RemoteLayout,
        ref: ExecutionHandleRef,
        *,
        log_path: Path,
        timeout_seconds: float | None,
        collect_spec: CollectSpec,
        poll_interval: float = 1.0,
        expected_owner: str | None = None,
    ) -> None:
        self._ssh = ssh
        self._layout = layout
        self.ref = ref
        self._log_path = log_path
        self._timeout = timeout_seconds
        self._collect = collect_spec
        self._interval = poll_interval
        self._expected_owner = expected_owner
        self._exit_code: int | None = None
        self._terminal = asyncio.Event()
        self._timed_out = False
        self._monitor: asyncio.Task | None = None
        self._log_offset = 0

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return self._exit_code

    def start(self) -> None:
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._run_monitor())

    async def _run_monitor(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = None if self._timeout is None else loop.time() + self._timeout
        backoff = self._interval
        while not self._terminal.is_set():
            try:
                await self._tail_log()
                status = await self._read_status()
                if status is not None:
                    self._exit_code = int(status["exit_code"])
                    self._terminal.set()
                    return
                backoff = self._interval
            except Exception:
                backoff = min(backoff * 2, 30.0)  # bounded backoff on transient fail
            if deadline is not None and loop.time() > deadline:
                self._timed_out = True
                await self._signal_group("KILL")
                self._exit_code = None
                self._terminal.set()
                return
            await asyncio.sleep(backoff)

    async def _tail_log(self) -> None:
        res = await self._ssh.run(["tail", "-c", f"+{self._log_offset + 1}", self._layout.log])
        if res.returncode == 0 and res.stdout:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(res.stdout)
            self._log_offset += len(res.stdout.encode("utf-8"))

    async def _read_status(self) -> dict | None:
        res = await self._ssh.run(["cat", self._layout.status])
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return json.loads(res.stdout)

    async def _pgid(self) -> int | None:
        for src in (self._layout.status, self._layout.pid):
            res = await self._ssh.run(["cat", src])
            if res.returncode == 0 and res.stdout.strip():
                with contextlib.suppress(Exception):
                    return int(json.loads(res.stdout)["pgid"])
        return None

    async def _signal_group(self, sig: str) -> None:
        pgid = await self._pgid()
        if pgid is not None:
            await self._ssh.run(["kill", f"-{sig}", f"-{pgid}"])

    async def wait(self) -> ExecutionResult:
        await self._terminal.wait()
        return ExecutionResult(
            exit_code=self._exit_code,
            output_log_path=self._log_path,
            timed_out=self._timed_out,
        )

    async def terminate(self, grace_seconds: float) -> None:
        await self._signal_group("TERM")
        await asyncio.sleep(grace_seconds)
        if not self._terminal.is_set():
            await self._signal_group("KILL")

    async def kill(self) -> None:
        await self._signal_group("KILL")

    async def collect(self) -> CollectResult:
        staging = self._collect.staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        pulled = await self._ssh.rsync(
            f"{self._ssh.host}:{self._layout.repo}/",
            f"{staging}/",
            delete=True,
            excludes=RSYNC_EXCLUDES_COLLECT,
        )
        if pulled.returncode != 0:
            raise RuntimeError(f"collect rsync failed: {pulled.stderr[:400]}")
        plan = plan_collect(
            self._collect.worktree, staging, self._collect.baseline,
            forbidden=[".git", ".maestro"],
        )
        apply_collect(
            self._collect.worktree, staging, plan, journal_dir=self._collect.journal_dir
        )
        return CollectResult(
            applied=True, files_changed=len(plan.modified) + len(plan.deleted)
        )

    async def cleanup(self) -> None:
        await self._verify_ownership()
        await self._ssh.run(["rm", "-rf", self._layout.root])
        for p in (self._collect.staging_dir, self._collect.journal_dir):
            shutil.rmtree(p, ignore_errors=True)
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor

    async def _verify_ownership(self) -> None:
        if self._expected_owner is None:
            return
        if not self._layout.root.startswith(self._ssh.workdir_root.rstrip("/") + "/"):
            raise RuntimeError(f"remote_dir {self._layout.root} escapes workdir_root")
        res = await self._ssh.run(["cat", self._layout.owner_marker])
        if res.returncode != 0 or res.stdout.strip() != self._expected_owner:
            raise RuntimeError(
                f"owner marker mismatch: refusing rm -rf {self._layout.root}"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_handle.py -v`
Expected: PASS (3).

- [ ] **Step 5: Add the byte-offset reconnect test**

```python
# append to tests/test_ssh_handle.py
@pytest.mark.anyio
async def test_tail_uses_byte_offset_no_dup(tmp_path):
    # First tail returns "abc", status still absent; second tail returns "" then status.
    seq = {"n": 0}
    def resp(argv, stdin):
        pass
    class Seq:
        calls = []
        async def __call__(self, argv, stdin):
            self.calls.append(argv)
            j = " ".join(argv)
            if "tail" in j:
                seq["n"] += 1
                return RunResult(0, "abc" if seq["n"] == 1 else "", "")
            if "cat" in j and ".status" in j:
                return RunResult(0, "" if seq["n"] < 2 else
                                 json.dumps({"pid":1,"pgid":1,"exit_code":0,"completed_at":1.0}), "")
            return RunResult(0, "", "")
    fake = Seq()
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = SshTaskHandle(
        ssh, remote_layout("/w", "e1"), _ref(),
        log_path=tmp_path / "log", timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    h.start()
    await h.wait()
    assert (tmp_path / "log").read_text() == "abc"  # written once, no duplication
    # second tail requested a higher offset
    tails = [c for c in fake.calls if "tail" in " ".join(c)]
    assert "+4" in " ".join(tails[1])
```

Run: `uv run pytest tests/test_ssh_handle.py -v`
Expected: PASS (4).

- [ ] **Step 6: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_handle.py tests/test_ssh_handle.py
git commit -m "feat(exec): SshTaskHandle + monitor (cached poll, byte-offset tail, pgroup signals)"
```

---

### Task 13 (E2): `SshBackend` + resolver wiring + run() launch sequence

**Files:**
- Create: `maestro/execution/ssh_backend.py`
- Modify: `maestro/execution/resolver.py` (`_build_ssh`, from Task A3)
- Test: `tests/test_ssh_backend.py` (create)

**Interfaces:**
- Consumes: `SshCli`, `ssh_launch`, `SshTaskHandle`, `secret_file.write_env_file`, resources supervisor path.
- Produces:
  - `SshBackend(name, transport, *, secret_env, runner=None, supervisor_src=<resource text>)` implementing `ExecutionBackend`.
  - `healthcheck()` = `ssh host true`; `can_run(req)` = probe each `required_tools` via `SshCli.probe_tool`.
  - `run(req)` performs: mktemp; git bundle → transfer → clone; rsync worktree overlay; write env-file locally + transfer; write descriptor + supervisor; `ssh python3 supervisor descriptor` and **await the handshake**; construct `SshTaskHandle`, `start()` it, return.
  - `probe(ref)` delegates to `ssh_recovery.probe_ssh` (Task E3).
  - resolver `_build_ssh(name, spec, transport)` returns `SshBackend(...)`.

- [ ] **Step 1: Write the failing test** (fake-runner: assert the launch order + handshake gate)

```python
# tests/test_ssh_backend.py
from pathlib import Path

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult


class Recorder:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.argvs.append(argv)
        j = " ".join(argv)
        if "maestro_supervisor.py" in j:
            return RunResult(0, "MAESTRO-SUPERVISOR-READY e\n", "")
        return RunResult(0, "", "")


def _req(tmp_path) -> ExecutionRequest:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "f.txt").write_text("x")
    return ExecutionRequest(
        run_id="api",
        argv=["spec-runner", "run", "--all"],
        workdir=wt,
        log_path=tmp_path / "api.log",
        collect=CollectPolicy(mode="whole_worktree"),
        required_tools=["spec-runner"],
        execution_id="e",
        entity_kind="workstream",
        backend_id="gpu",
    )


@pytest.mark.anyio
async def test_run_awaits_handshake_before_returning(tmp_path, monkeypatch):
    rec = Recorder()
    t = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    backend = SshBackend("gpu", t, secret_env=[], runner=rec)
    # Avoid real git/rsync: monkeypatch the transfer helpers to no-ops that
    # record. (The plan's implementation must route git-bundle/rsync through
    # small injectable seams — see Step 3.)
    monkeypatch.setattr(backend, "_materialize_remote", _fake_async)
    handle = await backend.run(_req(tmp_path))
    joined = [" ".join(a) for a in rec.argvs]
    assert any("maestro_supervisor.py" in j for j in joined)
    assert handle.os_pid is None  # remote


async def _fake_async(*a, **k):
    return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_backend.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_backend.py
"""SshBackend: a remote-over-SSH ExecutionBackend (Mode 2, bare isolation).

run() rsyncs a git-bundle-materialized worktree to a remote tmp dir, launches a
daemonizing Python supervisor, and returns only after its startup handshake so a
channel drop can never leave the run unobservable.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from maestro._vendor.obs import child_env
from maestro.execution.exec_config import SshTransport
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.execution.secret_file import write_env_file
from maestro.execution.ssh_cli import RunResult, Runner, SshCli
from maestro.execution.ssh_collect import capture_baseline
from maestro.execution.ssh_handle import CollectSpec, SshTaskHandle
from maestro.execution.ssh_launch import (
    RSYNC_EXCLUDES_OUT,
    build_descriptor,
    encode_transport_ref,
    remote_layout,
)

_HANDSHAKE = "MAESTRO-SUPERVISOR-READY"
_COLLECT_EXCLUDES = [".git", ".maestro", "*.log"]


def _supervisor_src() -> str:
    return (
        resources.files("maestro.execution.resources")
        .joinpath("maestro_supervisor.py")
        .read_text(encoding="utf-8")
    )


class SshBackend:
    def __init__(
        self,
        name: str,
        transport: SshTransport,
        *,
        secret_env: list[str],
        runner: Runner | None = None,
        local_staging_root: Path | None = None,
    ) -> None:
        self._name = name
        self._t = transport
        self._secret_env = secret_env
        self._ssh = SshCli(transport, runner=runner)
        self._staging_root = local_staging_root or Path(
            os.environ.get("TMPDIR", "/tmp")
        )

    @property
    def id(self) -> str:
        return self._name

    async def healthcheck(self) -> BackendHealth:
        if await self._ssh.check(["true"]):
            return BackendHealth(reachable=True)
        return BackendHealth(reachable=False, detail=f"ssh {self._t.host} unreachable")

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        missing = [t for t in req.required_tools if not await self._ssh.probe_tool(t)]
        return CapabilityResult(ok=not missing, missing_tools=missing)

    async def run(self, req: ExecutionRequest) -> SshTaskHandle:
        if req.execution_id is None:
            raise ValueError("SshBackend requires req.execution_id")
        layout = remote_layout(self._t.workdir_root, req.execution_id)
        baseline = capture_baseline(req.workdir, excludes=_COLLECT_EXCLUDES)

        await self._materialize_remote(req, layout)

        descriptor = build_descriptor(
            req.execution_id, layout, list(req.argv), self._t.workdir_root
        )
        # Launch the supervisor and block on its startup handshake, so a channel
        # drop after this point can never leave the run unobservable.
        result = await self._launch_supervisor(layout, descriptor)
        if _HANDSHAKE not in result.stdout:
            raise RuntimeError(f"supervisor handshake missing: {result.stderr[:400]}")

        ref = ExecutionHandleRef(
            backend_id=self._name,
            run_id=req.run_id,
            transport_ref=encode_transport_ref(
                self._t.host, self._t.port, layout.root, layout.status
            ),
            status_marker=layout.status,
            started_at=datetime.now(UTC),
        )
        staging = self._staging_root / f"maestro-collect-{req.execution_id}"
        journal = self._staging_root / f"maestro-journal-{req.execution_id}"
        handle = SshTaskHandle(
            self._ssh, layout, ref,
            log_path=req.log_path,
            timeout_seconds=req.timeout_seconds,
            collect_spec=CollectSpec(req.workdir, staging, journal, baseline),
            expected_owner=req.execution_id,
        )
        handle.start()
        return handle

    async def _launch_supervisor(self, layout, descriptor) -> RunResult:
        # Write descriptor + supervisor remotely (values are non-secret paths),
        # then launch. Descriptor content delivered over stdin to `tee`.
        await self._ssh.run(["mkdir", "-p", layout.root])
        await self._ssh.run(["tee", layout.descriptor], stdin=json.dumps(descriptor))
        await self._ssh.run(["tee", layout.supervisor], stdin=_supervisor_src())
        return await self._ssh.run(
            ["python3", layout.supervisor, layout.descriptor]
        )

    async def _materialize_remote(self, req: ExecutionRequest, layout) -> None:
        """git bundle → transfer → clone; rsync worktree overlay; env-file."""
        # 1. mktemp root
        await self._ssh.run(["mkdir", "-p", "-m", "700", layout.root])
        # 2. git bundle of the worktree HEAD, transferred and cloned.
        bundle = Path(self._staging_root) / f"maestro-bundle-{req.execution_id}.bundle"
        await _run_local(
            ["git", "-C", str(req.workdir), "bundle", "create", str(bundle), "HEAD"]
        )
        await self._ssh.rsync(
            str(bundle), f"{self._t.host}:{layout.root}/repo.bundle",
            delete=False, excludes=[],
        )
        await self._ssh.run(["git", "clone", f"{layout.root}/repo.bundle", layout.repo])
        # 3. overlay working tree (incl. dirty/untracked), excluding .git etc.
        await self._ssh.rsync(
            f"{req.workdir}/", f"{self._t.host}:{layout.repo}/",
            delete=False, excludes=RSYNC_EXCLUDES_OUT,
        )
        # 4. secret env-file (local 0600) → transfer.
        if self._secret_env:
            local_env = Path(self._staging_root) / f"maestro-env-{req.execution_id}"
            write_env_file(local_env, self._secret_env, os.environ)
            await self._ssh.rsync(
                str(local_env), f"{self._t.host}:{layout.env_file}",
                delete=False, excludes=[],
            )
            local_env.unlink(missing_ok=True)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        from maestro.execution.ssh_recovery import probe_ssh

        verdict = await probe_ssh(self._ssh, ref)
        return ProbeResult(alive=verdict.needs_review, detail=verdict.reason)


async def _run_local(argv: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {err.decode('utf-8', 'replace')[:400]}")
```

(Implementer note: keep `_materialize_remote` and `_launch_supervisor` as small overridable methods so the E2 unit test can monkeypatch `_materialize_remote` while the E-gated localhost e2e exercises them for real.)

Then replace the resolver stub `_build_ssh` (Task A3) with:

```python
    def _build_ssh(self, name, spec, transport):
        from maestro.execution.ssh_backend import SshBackend

        return SshBackend(
            name, transport, secret_env=self._execution.effective_secret_env(name)
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_backend.py -v && uv run pytest tests/test_backend_resolver_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_backend.py maestro/execution/resolver.py tests/test_ssh_backend.py
git commit -m "feat(exec): SshBackend run() with handshake-gated supervisor launch"
```

---

### Task 14 (E3): `ssh_recovery` — fail-closed probe classification

**Files:**
- Create: `maestro/execution/ssh_recovery.py`
- Test: `tests/test_ssh_recovery.py` (create)

**Interfaces:**
- Produces:
  - `RecoveryVerdict(needs_review: bool, reason: str)` (reuse the `docker_recovery` shape or import it).
  - `async probe_ssh(ssh: SshCli, ref: ExecutionHandleRef) -> RecoveryVerdict` — matrix: status marker present+terminal but handle not `collected` → review (tmp preserved); marker absent + pgroup alive → review; probe unreachable → review; marker present + already collected → safe (caller GCs).
  - `async gc_ssh_terminal(ssh, ref) -> str` — guarded remote `rm -rf` for a `collected` handle (ownership-checked), returns an outcome string.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_recovery.py
import json
from datetime import UTC, datetime

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_launch import remote_layout
from maestro.execution.ssh_recovery import probe_ssh


def _ref(host="gpu"):
    layout = remote_layout("/w", "e1")
    return ExecutionHandleRef(
        backend_id="gpu", run_id="api",
        transport_ref=json.dumps({"v":1,"transport":"ssh","host":host,"port":None,
                                  "remote_dir":layout.root,"status_marker":layout.status}),
        status_marker=layout.status, started_at=datetime.now(UTC),
    )


def _ssh(responses):
    async def runner(argv, stdin):
        for needle, r in responses:
            if needle in " ".join(argv):
                return r
        return RunResult(1, "", "")
    return SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=runner)


@pytest.mark.anyio
async def test_marker_absent_but_process_alive_needs_review():
    ssh = _ssh([(".status", RunResult(1, "", "no such file")),
                (".pid", RunResult(0, json.dumps({"pid":9,"pgid":9}), "")),
                ("kill -0", RunResult(0, "", ""))])
    v = await probe_ssh(ssh, _ref())
    assert v.needs_review


@pytest.mark.anyio
async def test_probe_unreachable_needs_review():
    ssh = _ssh([("", RunResult(255, "", "ssh: connect timeout"))])
    v = await probe_ssh(ssh, _ref())
    assert v.needs_review
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_recovery.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/ssh_recovery.py
"""Fail-closed SSH recovery classification (peer of docker_recovery).

A remote terminal marker is NOT a completed Maestro finalization: a crash after
the marker but before collect leaves unapplied changes in the remote worktree.
So probe_ssh routes anything uncertain — or terminal-but-not-collected — to
NEEDS_REVIEW and never deletes; only a caller holding a `collected` handle GCs.
"""

import contextlib
import json

from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import SshCli
from maestro.execution.docker_recovery import RecoveryVerdict


def _decode(ref: ExecutionHandleRef) -> dict:
    return json.loads(ref.transport_ref)


async def probe_ssh(ssh: SshCli, ref: ExecutionHandleRef) -> RecoveryVerdict:
    info = _decode(ref)
    status_marker = info["status_marker"]
    try:
        st = await ssh.run(["cat", status_marker])
    except Exception as e:
        return RecoveryVerdict(True, f"probe failed: {e}")
    if st.returncode == 0 and st.stdout.strip():
        # Terminal marker exists. finalize may not have collected → review,
        # remote tmp preserved. (Caller distinguishes `collected` via the DB
        # row and only then GCs.)
        return RecoveryVerdict(True, "terminal marker present; collect unconfirmed")
    # No marker: is the workload still alive?
    pgid = await _read_pgid(ssh, info)
    if pgid is not None and await ssh.check(["kill", "-0", f"-{pgid}"]):
        return RecoveryVerdict(True, "no marker but process group alive")
    if pgid is None:
        return RecoveryVerdict(True, "no marker and pgid unknown (fail-closed)")
    return RecoveryVerdict(True, "no marker; process group not confirmed dead")


async def _read_pgid(ssh: SshCli, info: dict) -> int | None:
    # pid file lives beside the status marker: <remote_dir>/<eid>.pid
    status = info["status_marker"]
    pid_file = status[: -len(".status")] + ".pid"
    res = await ssh.run(["cat", pid_file])
    if res.returncode == 0 and res.stdout.strip():
        with contextlib.suppress(Exception):
            return int(json.loads(res.stdout)["pgid"])
    return None


async def gc_ssh_terminal(ssh: SshCli, ref: ExecutionHandleRef) -> str:
    """Guarded remote rm -rf for a handle already known `collected`."""
    info = _decode(ref)
    remote_dir = info["remote_dir"]
    owner = f"{remote_dir}/.maestro-owner"
    res = await ssh.run(["cat", owner])
    if res.returncode != 0:
        return "no owner marker; skipped"
    await ssh.run(["rm", "-rf", remote_dir])
    return "removed"
```

(Implementer note: `_read_pgid` derives the pid path from `status_marker`. The probe is intentionally fail-closed — every non-clean branch is `needs_review=True`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_recovery.py -v`
Expected: PASS (2).

- [ ] **Step 5: Typecheck, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/execution/ssh_recovery.py tests/test_ssh_recovery.py
git commit -m "feat(exec): fail-closed SSH recovery probe + guarded GC"
```

---

## Increment F — Mode integration (scheduler guard + orchestrator wiring)

### Task 15 (F1): Scheduler passes `mode="scheduler"` (Mode-1 SSH fail-fast)

**Files:**
- Modify: `maestro/scheduler.py:280` (`self._backends = BackendResolver(execution)`)
- Test: `tests/test_scheduler_ssh_guard.py` (create)

**Interfaces:**
- Consumes: `BackendResolver(..., mode="scheduler")` (A3).
- Produces: a scheduler whose resolver rejects `ssh`-transport backends.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_ssh_guard.py
import pytest

from maestro.execution.exec_config import BackendSpec, ExecutionConfig, SshTransport
from maestro.execution.resolver import ExecutionConfigError
from maestro.scheduler import Scheduler


def test_scheduler_rejects_ssh_backend():
    cfg = ExecutionConfig(
        backends={
            "gpu": BackendSpec(
                transport=SshTransport(type="ssh", host="gpu", workdir_root="/w"),
                isolation={"type": "bare"},
            )
        }
    )
    sched = Scheduler(db_path=":memory:", execution=cfg)  # match the real ctor kwargs
    with pytest.raises(ExecutionConfigError, match="Mode-2"):
        sched._backends.resolve("gpu")
```

(Implementer: adapt the `Scheduler(...)` construction to the real constructor signature — the point under test is `sched._backends.resolve("gpu")` raising.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_ssh_guard.py -v`
Expected: FAIL — resolver built without `mode="scheduler"` does not raise.

- [ ] **Step 3: Write minimal implementation**

In `maestro/scheduler.py` at the resolver construction (`:280`):

```python
        self._backends = BackendResolver(execution, mode="scheduler")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler_ssh_guard.py -v`
Expected: PASS.

- [ ] **Step 5: Regression — scheduler local/docker paths unaffected**

Run: `uv run pytest tests/ -k "scheduler and not slow" -v` (targeted foreground; do NOT background the full suite)
Expected: PASS.

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/scheduler.py tests/test_scheduler_ssh_guard.py
git commit -m "feat(sched): enforce Mode-2-only SSH backends at resolver construction"
```

---

### Task 16 (F2): Orchestrator SSH request wiring (collect + mirror + finalize callbacks + recovery)

**Files:**
- Modify: `maestro/orchestrator.py` — request build (`:918-930`), `_monitor_running` (`:1044-1083`), `_update_progress` (`:1085-1108`), recovery pass (`:322-360`, `_probe_open_handle`).
- Test: `tests/test_orchestrator_ssh_wiring.py` (create)

**Interfaces:**
- Consumes: `SshBackend` (via resolver), `finalize.ensure_finalize_task(on_terminal, on_collected)` (B2), `ssh_recovery.probe_ssh`/`gc_ssh_terminal` (E3), `ssh_mirror` (D2).
- Produces:
  - For a non-local **ssh** backend, the `ExecutionRequest` carries `collect=CollectPolicy(mode="whole_worktree", conflict_policy="fail", on_failure="collect")` and a `ProgressMirrorPolicy(kind="spec_runner_sqlite", local_dir=<mirror>, interval_seconds=2, remote_globs=[])`.
  - `_update_progress` reads from `running.mirror_dir` when set (else the live remote spec dir — unchanged for local/docker).
  - `_monitor_running` supplies `on_terminal`/`on_collected` callbacks that persist `terminal`/`collected`, gates the success continuation on `fin.collect_succeeded`, and routes collect failure → `NEEDS_REVIEW` (remote tmp preserved).
  - Recovery `_probe_open_handle` branches on `backend_id`: docker → `probe_execution`; ssh → `probe_ssh`; both fail-closed to `NEEDS_REVIEW`.

- [ ] **Step 1: Write the failing test** (inject a fake backend; assert request shape + collect-failure routing)

```python
# tests/test_orchestrator_ssh_wiring.py
import pytest

from maestro.execution.models import CollectPolicy, ExecutionRequest


def test_ssh_request_uses_whole_worktree_collect_and_mirror():
    # Unit-level: the helper that builds the request for a non-local backend.
    from maestro.orchestrator import build_ssh_execution_request  # new pure helper

    req = build_ssh_execution_request(
        workstream_id="api",
        workspace="/tmp/wt",
        log_file="/tmp/api.log",
        cmd=["spec-runner", "run", "--all"],
        execution_id="e1",
        attempt=1,
        mirror_dir="/tmp/mirror",
    )
    assert isinstance(req, ExecutionRequest)
    assert req.collect.mode == "whole_worktree"
    assert req.collect.conflict_policy == "fail"
    assert req.progress_mirror is not None
    assert str(req.progress_mirror.local_dir) == "/tmp/mirror"
    assert req.required_tools == ["spec-runner"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_ssh_wiring.py -v`
Expected: FAIL — `build_ssh_execution_request` does not exist.

- [ ] **Step 3: Extract a pure request builder + branch the dispatch**

Add a module-level pure helper in `maestro/orchestrator.py` (keeps the dispatch testable and the local/docker path byte-for-byte unchanged):

```python
def build_ssh_execution_request(
    *,
    workstream_id: str,
    workspace: str,
    log_file: str,
    cmd: list[str],
    execution_id: str,
    attempt: int,
    mirror_dir: str,
) -> ExecutionRequest:
    """ExecutionRequest for a remote (ssh) Mode-2 workstream: whole-worktree
    collect + WAL-safe progress mirror. Secrets flow via the backend's
    secret_env allowlist (env-file), never inherit_env."""
    return ExecutionRequest(
        run_id=workstream_id,
        argv=cmd,
        workdir=Path(workspace),
        log_path=Path(log_file),
        inherit_env=False,
        collect=CollectPolicy(
            mode="whole_worktree", conflict_policy="fail", on_failure="collect"
        ),
        progress_mirror=ProgressMirrorPolicy(
            kind="spec_runner_sqlite",
            remote_globs=[],
            local_dir=Path(mirror_dir),
            interval_seconds=2.0,
        ),
        required_tools=["spec-runner"],
        execution_id=execution_id,
        entity_kind="workstream",
        attempt=attempt,
        backend_id="",  # set by the caller via model_copy (existing pattern)
    )
```

Import `ProgressMirrorPolicy` and `Path` at the top if not already present. In `_spawn_workstream`, after resolving the backend, branch: if `backend.id != "local"` and the resolved backend's transport is ssh (detect via `isinstance(backend, SshBackend)` — import it), build the request via `build_ssh_execution_request(...)` with `mirror_dir = self._log_dir / f"{workstream_id}.mirror"`, store `mirror_dir` on the `RunningWorkstream`. The docker/local branch keeps the existing `CollectPolicy(mode="none")` request verbatim.

- [ ] **Step 4: Add `mirror_dir` to `RunningWorkstream` and use it in `_update_progress`**

Add field `mirror_dir: Path | None = None` to `RunningWorkstream`. In `_update_progress`:

```python
        spec_dir = (
            running.mirror_dir
            if running.mirror_dir is not None
            else running.workspace_path / "spec"
        )
```

(The reader is unchanged; it just points at the mirror for ssh runs.)

- [ ] **Step 5: Callback-driven finalize + collect-failure routing in `_monitor_running`**

Replace the terminal branch of `_monitor_running` (`:1055-1079`) so DB phases persist between operations and collect failure routes to review:

```python
            if return_code is not None:
                eid = running.execution_id

                async def _mark_terminal() -> None:
                    if eid is not None:
                        await self._db.mark_execution_state(
                            eid, "terminal", allowed_from=["prepared", "running"]
                        )

                async def _mark_collected() -> None:
                    if eid is not None:
                        await self._db.mark_execution_state(
                            eid, "collected", allowed_from=["terminal"]
                        )

                fin = await asyncio.shield(
                    ensure_finalize_task(
                        running, on_terminal=_mark_terminal, on_collected=_mark_collected
                    )
                )
                if eid is not None and fin.cleaned:
                    await self._db.mark_execution_state(
                        eid, "cleaned", allowed_from=["collected"]
                    )
                if not fin.collect_succeeded:
                    # Remote tmp + staging preserved; do NOT enter PR/gate flow.
                    await self._transition(
                        zid,
                        WorkstreamStatus.NEEDS_REVIEW,
                        expected_status=WorkstreamStatus.RUNNING,
                        message="collect failed/conflict; remote workspace preserved",
                        error_message=(fin.collect_error or "collect failed"),
                    )
                    completed.append(zid)
                    continue
                await self._handle_completion(zid, running, fin.execution.exit_code)
                completed.append(zid)
```

(Docker/local: `collect()` no-ops succeed → `collect_succeeded=True`, `_handle_completion` runs exactly as before; `cleaned` now transitions from `collected` — this matches migration #8.)

- [ ] **Step 6: Recovery SSH branch**

In the recovery pass (`_probe_open_handle`, `:507`), branch on `row["backend_id"]`:

```python
        backend_id = row["backend_id"]
        if backend_id == "docker":
            verdict = await probe_execution(row["execution_id"], self._docker)
        else:
            # ssh (or any non-local, non-docker): probe via the backend.
            backend = self._backends.resolve(backend_id)
            ref = _handle_ref_from_row(row)  # small builder from the DB row
            probe = await backend.probe(ref)
            verdict = RecoveryVerdict(probe.alive, probe.detail)
        # ... existing NEEDS_REVIEW routing on verdict.needs_review ...
```

Add the ref-reconstruction helper (module-level in `orchestrator.py`):

```python
def _handle_ref_from_row(row: dict[str, Any]) -> ExecutionHandleRef:
    """Rebuild an ExecutionHandleRef from an execution_handles DB row."""
    return ExecutionHandleRef(
        backend_id=row["backend_id"],
        run_id=row["entity_id"],
        transport_ref=row["transport_ref"],
        status_marker=row.get("status_marker"),
        started_at=datetime.fromisoformat(row["created_at"]),
        workdir_mirror_path=None,
        state_mirror_path=None,
    )
```

For a `collected` row, GC via `gc_ssh_terminal(backend._ssh, ref)` and mark `cleaned` (allowed_from `["collected"]`) instead of routing to review — the SSH parallel of docker's `terminal → cleaned` GC sweep.

- [ ] **Step 6b: Observability spans (spec §14)**

The orchestrator already wraps the spawn in `with span("task.execute", ...)`. Extend it for remote runs so a dropped/remote executor still correlates: pass the backend id/host and add a nested transfer span around `_materialize_remote`/`collect` inside `SshBackend` (via the vendored `obs.span`):

```python
# in SshBackend.run(), around _materialize_remote:
from maestro._vendor.obs import span

with span("execution.dispatch", backend=self._name, host=self._t.host):
    with span("execution.transfer", direction="out"):
        await self._materialize_remote(req, layout)
```

And in `SshTaskHandle.collect()`, wrap the rsync-in with `span("execution.transfer", direction="in")`. `TRACEPARENT` already propagates via `child_env()` into the descriptor env (the supervisor loads it). No new deps — `obs.span` is the vendored lib already used across Maestro.

- [ ] **Step 7: Run the wiring test + orchestrator regression**

Run: `uv run pytest tests/test_orchestrator_ssh_wiring.py -v`
Expected: PASS. Then targeted foreground:
Run: `uv run pytest tests/ -k "orchestrator and (monitor or recovery or spawn or finalize)" -v`
Expected: PASS (local/docker paths unchanged).

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/orchestrator.py tests/test_orchestrator_ssh_wiring.py
git commit -m "feat(orch): wire SSH backend — collect+mirror request, phased finalize, ssh recovery"
```

---

## Increment G — Docs, example, gated e2e

### Task 17 (G1): CLAUDE.md drift note + example `with-ssh.yaml`

**Files:**
- Modify: `maestro/CLAUDE.md` (the "state polling deprecated" line, ~`:155`)
- Create: `examples/with-ssh.yaml`
- Test: `tests/test_examples_valid.py` (extend if it exists, else create) — load & validate the example.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_examples_valid.py  (add this case)
from maestro.config import load_orchestrator_config


def test_with_ssh_example_loads_and_resolves():
    cfg = load_orchestrator_config("examples/with-ssh.yaml")
    reg = cfg.execution.normalized()
    assert reg["gpu-box"].transport.type == "ssh"
    assert "spec-runner" not in reg  # sanity: only declared backends + local
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_examples_valid.py -k with_ssh -v`
Expected: FAIL — example file missing.

- [ ] **Step 3: Write the example**

```yaml
# examples/with-ssh.yaml — Mode 2 (orchestrator) with a remote SSH executor.
name: with-ssh-demo
repo_path: .
base_branch: main

execution:
  default_backend: local
  secret_env_defaults:
    - ANTHROPIC_API_KEY
  backends:
    gpu-box:
      transport:
        type: ssh
        host: gpu-box            # ssh config alias or bare hostname (NOT user@host:port)
        workdir_root: /var/tmp/maestro
        connect_timeout_s: 10
      isolation:
        type: bare
      secret_env:
        - ANTHROPIC_API_KEY

workstreams:
  - id: api
    description: Build the API surface on the remote executor.
    scope: ["src/api/**"]
    backend: gpu-box
```

- [ ] **Step 4: Update the CLAUDE.md drift note**

In `maestro/CLAUDE.md`, change the "callbacks from spec-runner, state polling deprecated" line to note polling is deliberately reintroduced for remote/NAT executors (a WAL-safe SQLite snapshot mirror), citing the SSH backend.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_examples_valid.py -k with_ssh -v`
Expected: PASS.

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add examples/with-ssh.yaml maestro/CLAUDE.md tests/test_examples_valid.py
git commit -m "docs(exec): with-ssh.yaml example + CLAUDE.md polling drift note"
```

---

### Task 18 (G2): Gated localhost-SSH e2e (real cleanup exercised)

**Files:**
- Create: `tests/e2e/test_ssh_localhost_e2e.py`
- Test: itself (opt-in gate).

**Interfaces:** none new — exercises `SshBackend` end-to-end over `ssh localhost`.

**Gate:** skip unless `MAESTRO_SSH_E2E=1` **and** `ssh -o BatchMode=yes localhost true` succeeds (mirrors the docker integration gate). CI/dev without passwordless localhost sshd skips cleanly.

- [ ] **Step 1: Write the gated e2e**

```python
# tests/e2e/test_ssh_localhost_e2e.py
import os
import subprocess

import pytest

pytestmark = pytest.mark.anyio

_GATED = os.environ.get("MAESTRO_SSH_E2E") != "1" or (
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "localhost", "true"],
        capture_output=True,
    ).returncode
    != 0
)
skip_reason = "set MAESTRO_SSH_E2E=1 and enable passwordless `ssh localhost`"


@pytest.mark.skipif(_GATED, reason=skip_reason)
async def test_localhost_run_collect_and_real_cleanup(tmp_path):
    from maestro.execution.exec_config import SshTransport
    from maestro.execution.models import CollectPolicy, ExecutionRequest
    from maestro.execution.ssh_backend import SshBackend

    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    (wt / "a.txt").write_text("orig")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=wt, check=True)

    workdir_root = tmp_path / "remote"
    workdir_root.mkdir()
    t = SshTransport(type="ssh", host="localhost", workdir_root=str(workdir_root))
    backend = SshBackend("localhost", t, secret_env=[])

    # A workload that mutates the worktree so collect has real changes.
    req = ExecutionRequest(
        run_id="ws", argv=["sh", "-c", "echo changed > a.txt; echo new > b.txt"],
        workdir=wt, log_path=tmp_path / "ws.log",
        collect=CollectPolicy(mode="whole_worktree"),
        required_tools=[], execution_id="e2e1", entity_kind="workstream",
        backend_id="localhost",
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    await handle.collect()
    assert (wt / "a.txt").read_text().strip() == "changed"
    assert (wt / "b.txt").read_text().strip() == "new"
    # Real guarded cleanup over localhost SSH: remote tmp actually removed.
    remote_root = workdir_root / "maestro-exec-e2e1"
    assert remote_root.exists()
    await handle.cleanup()
    assert not remote_root.exists()
```

- [ ] **Step 2: Run it in gated mode (opt-in)**

Run (only where localhost sshd is available):
`MAESTRO_SSH_E2E=1 uv run pytest tests/e2e/test_ssh_localhost_e2e.py -v`
Expected: PASS (or SKIP where the gate is off). In the shared workspace, this is a **targeted foreground** run — never backgrounded.

- [ ] **Step 3: Verify it SKIPS cleanly without the gate**

Run: `uv run pytest tests/e2e/test_ssh_localhost_e2e.py -v`
Expected: SKIPPED (1).

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add tests/e2e/test_ssh_localhost_e2e.py
git commit -m "test(exec): gated localhost-SSH e2e — run, collect, real guarded cleanup"
```

---

## Final verification (before PR)

- [ ] `uv run pyrefly check` — clean.
- [ ] `uv run ruff format . && uv run ruff check .` — clean.
- [ ] Targeted foreground suites (never background the whole suite — watchdog kills long bg `pytest`):
  - `uv run pytest tests/ -k "exec_config or resolver or secret_file or finalize" -v`
  - `uv run pytest tests/ -k "ssh_cli or ssh_launch or supervisor or ssh_collect or ssh_mirror" -v`
  - `uv run pytest tests/ -k "ssh_handle or ssh_backend or ssh_recovery" -v`
  - `uv run pytest tests/ -k "database or migration or orchestrator or scheduler" -v`
- [ ] Full suite on **PR CI** (not locally in the shared workspace).
- [ ] Open PR from `feat/ssh-backend-phase2a`; address GitHub Copilot review; **do not self-merge** (user merges).

