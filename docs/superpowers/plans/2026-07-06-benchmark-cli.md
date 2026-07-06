# `maestro benchmark` CLI (R-06b M5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `maestro benchmark <benchmark-id> --agent <harness>` — run one ATP benchmark against one local agent and print/report the result, wiring the existing M1-M4 pieces.

**Architecture:** One inline `@app.command("benchmark")` in `maestro/cli.py` (house pattern for single commands) plus two private async helpers in the same file: `_benchmark_flow` (adapter ctx + runner + report dispatch) and `_report_with_lifecycle` (ArbiterClient start→report→stop, fire-and-forget). No changes inside `maestro/benchmark/`.

**Tech Stack:** Typer + Rich, asyncio.run, existing `maestro.benchmark` package (`BenchmarkRunner`, `MaestroATPAdapter`, `SpawnerResponder`, `report_benchmark_to_arbiter`), vendored `ArbiterClient`. Spec: `docs/superpowers/specs/2026-07-06-benchmark-cli-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; anyio for async tests (these CLI tests are sync CliRunner).
- Allowed `--agent` set is EXPLICIT: `claude_code`, `codex_cli`, `aider`, `opencode`. `auto` and `announce` are rejected with their own distinct messages (exit 1) BEFORE any ATP contact.
- `report_status` values are the M4 model contract `Literal["ok", "failed", "skipped"]` — the CLI never extends the model.
- Arbiter lifecycle: `start()` → `report_benchmark_to_arbiter` → `finally: stop()`; `start()` failure = report failure (`report_status="failed"`, exit still 0); `stop()` awaited on every path.
- `--json`: stdout is byte-for-byte the `BenchmarkResult` JSON; workdir note, summary, report notes, errors ALL go to stderr.
- Exit codes: 0 = run completed (task errors don't fail the command); 1 = infra failure (bad agent, unavailable CLI, ATP errors); 2 = `--timeout <= 0`. Message-not-traceback contract throughout.
- ATP endpoint precedence: `--atp-url` > `$MAESTRO_ATP_BASE_URL` > `http://localhost:8000`.
- Branch: `feat/benchmark-cli` (exists, spec committed). Full suite (~1440) green at every commit.

---

### Task 1: Core command — validation, run, output

**Files:**
- Modify: `maestro/cli.py` (new command + `_benchmark_flow` helper; imports)
- Test: `tests/test_cli_benchmark.py` (new)

**Interfaces:**
- Consumes: `MaestroATPAdapter.from_env(platform_url=...)`, `SpawnerResponder(spawner, workdir, log_dir, timeout_seconds)`, `BenchmarkRunner(client, agent).run(benchmark_id, run_id=...)`, `BenchmarkResult` (all existing).
- Produces (Task 2 relies on): `_benchmark_flow(adapter, responder, benchmark_id, run_id, arbiter_bin: str | None, no_report: bool, notes: Console) -> BenchmarkResult` — in Task 1 the arbiter branch only prints the skipped note; module-level `_bench_spawner_for(agent: str) -> AgentSpawner` factory (tests monkeypatch it); module-level `_ALLOWED_BENCH_AGENTS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_benchmark.py`:

```python
"""Tests for `maestro benchmark` (R-06b M5)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import maestro.cli as cli_mod
from maestro.benchmark.models import BenchmarkResult
from maestro.cli import app
from maestro.models import Task
from maestro.spawners.base import AgentSpawner


runner = CliRunner()


class FakeTask:
    def __init__(self, task_index: int, prompt: str) -> None:
        self.task_index = task_index
        self.prompt = prompt


class FakeRun:
    """Two-task fake BenchmarkRun."""

    def __init__(self) -> None:
        self.run_id = "fake-run-1"
        self.submitted: list[tuple[int, str]] = []

    async def tasks(self):
        yield FakeTask(0, "prompt zero")
        yield FakeTask(1, "prompt one")

    async def submit(self, task_index: int, response: str) -> None:
        self.submitted.append((task_index, response))

    async def finalize(self) -> tuple[float, dict[str, float]]:
        return 0.75, {"accuracy": 0.75}


class FakeAdapter:
    """Stands in for MaestroATPAdapter."""

    instances: list["FakeAdapter"] = []

    def __init__(self, platform_url: str) -> None:
        self.platform_url = platform_url
        self.started: list[tuple[str, str]] = []
        self.run_ids_requested: list[str | None] = []
        FakeAdapter.instances.append(self)

    @classmethod
    def from_env(cls, platform_url: str = "http://localhost:8000", **_: object):
        return cls(platform_url)

    async def start_run(self, benchmark_id: str, agent_name: str) -> FakeRun:
        self.started.append((benchmark_id, agent_name))
        return FakeRun()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeBenchSpawner(AgentSpawner):
    """Writes a claude-format log so cost parsing yields tokens."""

    def __init__(self, agent_type_str: str = "claude_code") -> None:
        self._agent_type = agent_type_str

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def is_available(self) -> bool:
        return True

    def spawn(self, task: Task, context, workdir, log_file, retry_context="", *, model=None):
        import subprocess

        log_file.write_text(
            '{"result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5}}',
            encoding="utf-8",
        )
        return subprocess.Popen(["true"])


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch):
    FakeAdapter.instances = []
    monkeypatch.setattr(cli_mod, "MaestroATPAdapter", FakeAdapter)
    monkeypatch.setattr(
        cli_mod, "_bench_spawner_for", lambda agent: FakeBenchSpawner(agent)
    )
    monkeypatch.delenv("MAESTRO_ARBITER_BIN", raising=False)
    monkeypatch.delenv("MAESTRO_ATP_BASE_URL", raising=False)


class TestAgentValidation:
    def test_auto_rejected_before_atp(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "auto"])
        assert result.exit_code == 1
        assert "routing sentinel" in result.output
        assert FakeAdapter.instances == []

    def test_announce_rejected_before_atp(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "announce"])
        assert result.exit_code == 1
        assert "no-op echo" in result.output
        assert FakeAdapter.instances == []

    def test_unknown_agent_names_allowed_set(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "nosuch"])
        assert result.exit_code == 1
        assert "claude_code" in result.output and "opencode" in result.output
        assert FakeAdapter.instances == []

    def test_unavailable_agent_cli_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Unavailable(FakeBenchSpawner):
            def is_available(self) -> bool:
                return False

        monkeypatch.setattr(
            cli_mod, "_bench_spawner_for", lambda agent: Unavailable(agent)
        )
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "claude_code"])
        assert result.exit_code == 1
        assert "not found in PATH" in result.output
        assert FakeAdapter.instances == []

    def test_timeout_zero_rejected(self) -> None:
        result = runner.invoke(
            app, ["benchmark", "b1", "--agent", "claude_code", "--timeout", "0"]
        )
        assert result.exit_code == 2
        assert FakeAdapter.instances == []


class TestHappyPath:
    def test_run_prints_score_and_tasks(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "benchmark",
                "swe-mini",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
            ],
        )
        assert result.exit_code == 0
        assert "0.75" in result.output
        assert "prompt zero"[:6] not in ("",)  # noqa: PLR0133 — guard removed below
        assert "swe-mini" in result.output
        assert str(tmp_path / "wd") in result.output  # workdir announced
        assert "skipped" in result.output  # arbiter note (env unset)
        adapter = FakeAdapter.instances[0]
        assert adapter.started == [("swe-mini", "claude_code")]

    def test_json_stdout_is_pure_json(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "benchmark",
                "swe-mini",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
                "--json",
            ],
        )
        assert result.exit_code == 0
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.benchmark_id == "swe-mini"
        assert parsed.score == 0.75
        assert len(parsed.per_task) == 2

    def test_run_id_forwarded(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "benchmark",
                "swe-mini",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
                "--run-id",
                "ci-42",
                "--json",
            ],
        )
        assert result.exit_code == 0
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.run_id == "ci-42"


class TestAtpUrl:
    def test_flag_beats_env_beats_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wd = str(tmp_path / "wd")
        runner.invoke(app, ["benchmark", "b", "--agent", "aider", "--workdir", wd])
        assert FakeAdapter.instances[-1].platform_url == "http://localhost:8000"

        monkeypatch.setenv("MAESTRO_ATP_BASE_URL", "http://atp.example:9000")
        runner.invoke(app, ["benchmark", "b", "--agent", "aider", "--workdir", wd])
        assert FakeAdapter.instances[-1].platform_url == "http://atp.example:9000"

        runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "aider",
                "--workdir",
                wd,
                "--atp-url",
                "http://flag.example:1234",
            ],
        )
        assert FakeAdapter.instances[-1].platform_url == "http://flag.example:1234"


class TestAtpFailure:
    def test_start_run_failure_is_message_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FailingAdapter(FakeAdapter):
            async def start_run(self, benchmark_id: str, agent_name: str):
                raise RuntimeError("401 Unauthorized")

        monkeypatch.setattr(cli_mod, "MaestroATPAdapter", FailingAdapter)
        result = runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
            ],
        )
        assert result.exit_code == 1
        assert "401 Unauthorized" in result.output
        assert "ATP_TOKEN" in result.output  # resolution-chain hint
        assert "Traceback" not in result.output
```

Remove the stray placeholder assertion line (`assert "prompt zero"[:6] not in ("",)`) — replace it with a real per-task presence check: `assert "prompt" in result.output` (the table shows task rows). Write the final test file WITHOUT that noqa line.

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_cli_benchmark.py -q`
Expected: FAIL — no `benchmark` command / no `MaestroATPAdapter` attribute on `maestro.cli`.

- [ ] **Step 3: Implement in `maestro/cli.py`**

Imports (extend existing import block):

```python
import tempfile

from maestro.benchmark import BenchmarkRunner, MaestroATPAdapter, SpawnerResponder
from maestro.benchmark.models import BenchmarkResult
```

(Verify these names are exported from `maestro/benchmark/__init__.py`; import from submodules if not.)

Module-level helpers (near the other module-level constants):

```python
_ALLOWED_BENCH_AGENTS = ("claude_code", "codex_cli", "aider", "opencode")


def _bench_spawner_for(agent: str) -> AgentSpawner:
    """Fresh spawner for a benchmark run. Module-level for test monkeypatching."""
    from maestro.spawners import (
        AiderSpawner,
        ClaudeCodeSpawner,
        CodexSpawner,
        OpencodeSpawner,
    )

    factories: dict[str, type[AgentSpawner]] = {
        "claude_code": ClaudeCodeSpawner,
        "codex_cli": CodexSpawner,
        "aider": AiderSpawner,
        "opencode": OpencodeSpawner,
    }
    return factories[agent]()


async def _benchmark_flow(
    adapter,
    responder,
    benchmark_id: str,
    run_id: str | None,
    arbiter_bin: str | None,
    no_report: bool,
    notes: Console,
) -> BenchmarkResult:
    """Run the benchmark, then dispatch the (optional) arbiter report."""
    async with adapter:
        result = await BenchmarkRunner(adapter, responder).run(
            benchmark_id, run_id=run_id
        )
    if no_report:
        notes.print("arbiter report skipped (--no-report)")
        return result
    if not arbiter_bin:
        notes.print("arbiter report skipped (MAESTRO_ARBITER_BIN unset)")
        return result
    return await _report_with_lifecycle(result, arbiter_bin, notes)
```

For Task 1, `_report_with_lifecycle` is a stub that will be replaced in Task 2:

```python
async def _report_with_lifecycle(
    result: BenchmarkResult, arbiter_bin: str, notes: Console
) -> BenchmarkResult:
    raise NotImplementedError  # Task 2 (unreachable in Task 1: gated on env)
```

The command:

```python
@app.command("benchmark")
def benchmark(
    benchmark_id: str = typer.Argument(..., help="ATP benchmark id to run"),
    agent: str = typer.Option(
        ...,
        "--agent",
        help="Harness: claude_code | codex_cli | aider | opencode. "
        "Model comes from MAESTRO_<HARNESS>_MODEL / the catalog default.",
    ),
    workdir: Path | None = typer.Option(
        None, "--workdir", help="Working dir (default: fresh temp dir; kept)"
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", help="Per-task timeout in seconds (must be > 0)"
    ),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Explicit run id (CI retry idempotency)"
    ),
    atp_url: str | None = typer.Option(
        None,
        "--atp-url",
        help="ATP base URL (default: $MAESTRO_ATP_BASE_URL, else "
        "http://localhost:8000)",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print BenchmarkResult JSON on stdout (notes → stderr)"
    ),
    no_report: bool = typer.Option(
        False, "--no-report", help="Skip arbiter reporting even if configured"
    ),
) -> None:
    """Run one ATP benchmark against one local agent harness (R-06b M5).

    Exit codes: 0 = run completed (per-task errors live in the score and
    the table, not the exit code); 1 = infrastructure failure; 2 = bad
    --timeout. With MAESTRO_ARBITER_BIN set, the result is reported to the
    arbiter fire-and-forget (a report failure never fails the run).
    """
    err = Console(stderr=True)
    # With --json, stdout must stay byte-for-byte JSON: ALL notes → stderr.
    notes = err if json_output else console

    if agent == "auto":
        err.print(
            "[red]--agent auto is a routing sentinel[/red] — pick a concrete "
            f"harness: {', '.join(_ALLOWED_BENCH_AGENTS)}"
        )
        raise typer.Exit(1)
    if agent == "announce":
        err.print(
            "[red]announce is a no-op echo harness[/red] — benchmarking it "
            "would record a fake success as routing signal"
        )
        raise typer.Exit(1)
    if agent not in _ALLOWED_BENCH_AGENTS:
        err.print(
            f"[red]unknown agent {agent!r}[/red] — allowed: "
            f"{', '.join(_ALLOWED_BENCH_AGENTS)}"
        )
        raise typer.Exit(1)

    if timeout <= 0:
        err.print("[red]--timeout must be > 0[/red]")
        raise typer.Exit(2)

    spawner = _bench_spawner_for(agent)
    if not spawner.is_available():
        err.print(f"[red]agent CLI '{agent}' not found in PATH[/red]")
        raise typer.Exit(1)

    wd = workdir or Path(tempfile.mkdtemp(prefix="maestro-bench-"))
    wd.mkdir(parents=True, exist_ok=True)
    log_dir = wd / "logs"
    log_dir.mkdir(exist_ok=True)
    # Announce BEFORE the run: on a crash the partial logs must be findable.
    notes.print(f"workdir: {wd}")

    url = atp_url or os.environ.get("MAESTRO_ATP_BASE_URL") or "http://localhost:8000"
    adapter = MaestroATPAdapter.from_env(platform_url=url)
    responder = SpawnerResponder(
        spawner, workdir=wd, log_dir=log_dir, timeout_seconds=timeout
    )
    arbiter_bin = os.environ.get("MAESTRO_ARBITER_BIN")

    try:
        result = asyncio.run(
            _benchmark_flow(
                adapter,
                responder,
                benchmark_id,
                run_id,
                arbiter_bin,
                no_report,
                notes,
            )
        )
    except Exception as exc:
        err.print(f"[red]benchmark failed[/red]: {exc}")
        err.print(
            "hint: check the ATP endpoint (--atp-url / $MAESTRO_ATP_BASE_URL) "
            "and token (ATP_TOKEN env or ~/.atp/config.json)"
        )
        raise typer.Exit(1) from exc

    _print_benchmark_summary(result, wd, notes)
    if json_output:
        # sys.stdout directly: byte-for-byte JSON, no Rich wrapping.
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
```

Summary renderer (same file):

```python
def _print_benchmark_summary(
    result: BenchmarkResult, wd: Path, notes: Console
) -> None:
    notes.print(
        f"benchmark [bold]{result.benchmark_id}[/bold] | agent "
        f"{result.agent_id} | run {result.run_id}"
    )
    notes.print(
        f"score: [bold]{result.score}[/bold]"
        + (f" | components: {result.score_components}" if result.score_components else "")
    )
    table = Table(title="Tasks")
    table.add_column("#")
    table.add_column("duration s")
    table.add_column("tokens")
    table.add_column("cost")
    table.add_column("error")
    for t in result.per_task:
        table.add_row(
            str(t.task_index),
            f"{t.duration_seconds:.1f}",
            str(t.tokens_used) if t.tokens_used is not None else "-",
            f"{t.cost_usd:.4f}" if t.cost_usd is not None else "-",
            t.error or "",
        )
    notes.print(table)
    notes.print(
        f"totals: tokens={result.total_tokens} cost={result.total_cost_usd} "
        f"duration={result.duration_seconds:.1f}s"
    )
    notes.print(
        f"arbiter report: {result.report_status}"
        + (f" ({result.report_error})" if result.report_error else "")
    )
    notes.print(f"logs: {wd / 'logs'}")
```

(Check cli.py's existing imports for `Table`, `sys`, `os`, `asyncio`, `Console` — extend, don't duplicate. `console` is the existing module-level stdout console.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cli_benchmark.py -q && uv run pytest tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/cli.py tests/test_cli_benchmark.py
git commit -m "feat(cli): maestro benchmark — run one ATP benchmark against one harness (R-06b M5 core)

Allowlist {claude_code,codex_cli,aider,opencode}; auto and announce
rejected with distinct messages before any ATP contact; is_available
checked before spending tokens; --json keeps stdout byte-for-byte JSON."
```

---

### Task 2: Arbiter report lifecycle

**Files:**
- Modify: `maestro/cli.py` (replace the `_report_with_lifecycle` stub)
- Test: `tests/test_cli_benchmark.py` (extend)

**Interfaces:**
- Consumes: `report_benchmark_to_arbiter(result, client) -> BenchmarkResult` (returns a copy with `report_status`/`report_error`); vendored `ArbiterClient(ArbiterClientConfig)` with `await start()` / `await stop()`; smoke-script path convention: `arbiter_repo = Path(bin).parent.parent.parent`, `config_dir = repo/"config"`, `tree_path = repo/"models/agent_policy_tree.json"`.
- Produces: working `MAESTRO_ARBITER_BIN` path in the CLI.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_benchmark.py`:

```python
class FakeArbiterClient:
    """Records lifecycle calls; behavior configured by class attrs."""

    instances: list["FakeArbiterClient"] = []
    fail_start = False

    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[str] = []
        FakeArbiterClient.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")
        if FakeArbiterClient.fail_start:
            raise RuntimeError("arbiter binary refused to start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def report_benchmark_raw(self, payload: dict) -> dict:
        self.calls.append("report")
        return {"status": "created"}


@pytest.fixture()
def _arbiter_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """MAESTRO_ARBITER_BIN pointing at a plausible repo layout."""
    repo = tmp_path / "arbiter-repo"
    bin_path = repo / "target" / "release" / "arbiter-mcp"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "models").mkdir()
    (repo / "models" / "agent_policy_tree.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MAESTRO_ARBITER_BIN", str(bin_path))
    FakeArbiterClient.instances = []
    FakeArbiterClient.fail_start = False
    monkeypatch.setattr(cli_mod, "ArbiterClient", FakeArbiterClient)
    return bin_path


class TestArbiterReport:
    def _invoke(self, tmp_path: Path, *extra: str):
        return runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
                *extra,
            ],
        )

    def test_lifecycle_start_report_stop(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        result = self._invoke(tmp_path, "--json")
        assert result.exit_code == 0
        client = FakeArbiterClient.instances[0]
        assert client.calls == ["start", "report", "stop"]
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.report_status == "ok"

    def test_start_failure_is_report_failure_not_run_failure(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        FakeArbiterClient.fail_start = True
        result = self._invoke(tmp_path, "--json")
        assert result.exit_code == 0  # fire-and-forget
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.report_status == "failed"
        assert "refused to start" in (parsed.report_error or "")
        client = FakeArbiterClient.instances[0]
        assert client.calls[0] == "start"
        assert client.calls[-1] == "stop"  # stop() on the failure path too

    def test_no_report_skips_client_entirely(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        result = self._invoke(tmp_path, "--no-report")
        assert result.exit_code == 0
        assert FakeArbiterClient.instances == []
        assert "skipped (--no-report)" in result.output

    def test_env_unset_skips_with_note(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path)
        assert result.exit_code == 0
        assert "MAESTRO_ARBITER_BIN unset" in result.output
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_cli_benchmark.py -q`
Expected: new tests FAIL (`NotImplementedError` stub / missing `ArbiterClient` attr on cli module).

- [ ] **Step 3: Implement**

In `maestro/cli.py`, import the client (extend imports):

```python
from maestro.benchmark import report_benchmark_to_arbiter
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
```

Replace the stub:

```python
async def _report_with_lifecycle(
    result: BenchmarkResult, arbiter_bin: str, notes: Console
) -> BenchmarkResult:
    """M4 fire-and-forget report with explicit client lifecycle.

    start() failure counts as a report failure (report_status="failed"),
    never as a run failure; stop() is awaited on every path so the
    subprocess can't leak. Paths follow the smoke-script convention:
    the binary lives at <repo>/target/release/arbiter-mcp.
    """
    bin_path = Path(arbiter_bin)
    repo = bin_path.parent.parent.parent
    config = ArbiterClientConfig(
        binary_path=str(bin_path),
        config_dir=str(repo / "config"),
        tree_path=str(repo / "models" / "agent_policy_tree.json"),
    )
    client = ArbiterClient(config)
    started = False
    try:
        await client.start()
        started = True
        result = await report_benchmark_to_arbiter(result, client)
    except Exception as exc:  # start() failure = report failure, not run failure
        result = result.model_copy(
            update={"report_status": "failed", "report_error": str(exc)}
        )
    finally:
        if started:
            await client.stop()
        else:
            # stop() must still run for a partially-started client; the
            # vendored client tolerates stop() after failed start.
            try:
                await client.stop()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
    notes.print(f"arbiter report: {result.report_status}")
    return result
```

Simplify the finally block to a single unconditional best-effort stop (drop the `started` flag) IF the vendored client's `stop()` is verified idempotent/safe after failed `start()` — check `maestro/coordination/arbiter_client.py` `stop()` implementation; if it guards internally, the plan's simpler form is:

```python
    finally:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
```

Use whichever matches the client's actual contract; the TESTS pin the behavior (stop always called).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cli_benchmark.py -q`
Expected: PASS (all Task 1 tests too).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/cli.py tests/test_cli_benchmark.py
git commit -m "feat(cli): benchmark reports to arbiter with explicit client lifecycle

start -> report -> finally stop; start() failure is a fire-and-forget
report failure (exit stays 0, report_status=failed), never a run
failure. Paths derive from MAESTRO_ARBITER_BIN via the smoke-script
repo-layout convention."
```

---

### Task 3: Docs, TODO tick, final gates

**Files:**
- Modify: `CLAUDE.md` (Development Commands), `TODO.md` (M5 item)

**Interfaces:** consumes everything above.

- [ ] **Step 1: CLAUDE.md**

In Development Commands, after the model-catalog block:

```markdown
# === Agent benchmarking (R-06b M5) ===
uv run maestro benchmark swe-mini --agent claude_code            # Run one ATP benchmark
uv run maestro benchmark swe-mini --agent opencode --json        # Machine output (stdout = JSON)
# MAESTRO_ARBITER_BIN set -> result reported to arbiter (fire-and-forget)
```

- [ ] **Step 2: TODO.md**

Tick `R-06b M5 CLI` (`- [x] **R-06b M5 CLI**: ... (closed by feat/benchmark-cli)`), preserving the original text.

- [ ] **Step 3: Final gates + help smoke**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
uv run maestro benchmark --help
uv run maestro benchmark some-bench --agent announce; echo "exit: $? (expect 1)"
```

Expected: suite green (~1460), help renders the exit-code contract, announce rejected with the fake-signal message.

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: maestro benchmark CLI shipped — R-06b M5 ticked"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final whole-branch review)

```bash
git push -u origin feat/benchmark-cli
gh pr create --title "feat(cli): maestro benchmark — R-06b M5" --body "$(cat <<'EOF'
## Summary
- `maestro benchmark <id> --agent <harness>` wires the existing M1-M4 pieces (BenchmarkRunner, MaestroATPAdapter, SpawnerResponder, report_benchmark_to_arbiter) into a user-facing command — no new benchmark logic
- Explicit harness allowlist {claude_code, codex_cli, aider, opencode}; auto/announce rejected with distinct messages BEFORE any ATP contact; is_available checked before spending tokens
- Arbiter report gated on MAESTRO_ARBITER_BIN (+ --no-report): explicit start→report→finally-stop lifecycle; start() failure = report failure (exit stays 0), subprocess never leaks
- report_status stays the M4 model contract (ok/failed/skipped) — no model extension
- --json: stdout is byte-for-byte BenchmarkResult JSON; every note/summary/error goes to stderr
- --atp-url / $MAESTRO_ATP_BASE_URL endpoint override (SDK from_env hard-codes localhost)

Spec: docs/superpowers/specs/2026-07-06-benchmark-cli-design.md

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] Lifecycle tests: start→report→stop order, start-failure → report_status=failed + stop still awaited
- [ ] Gating: env unset / --no-report / env set
- [ ] Validation: auto, announce, unknown, unavailable — all pre-ATP; --timeout 0 → exit 2
- [ ] --json purity; --run-id forwarding; endpoint precedence flag > env > default

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: agent selection/allowlist → Task 1; workdir/timeout → Task 1; run + ATP url + whole-flow error handling → Task 1; arbiter lifecycle + status model → Task 2; output/exit codes → Task 1; docs → Task 3.
- The `--timeout` check is a body check (exit 2), not Typer `min=` — Typer's `min=` produces click's own exit-2 usage error anyway; body check keeps the message ours. Test asserts exit 2 either way.
- Placeholder scan: Task 1 Step 1 contained one stray placeholder assertion — the step text explicitly instructs replacing it before writing the file.
- Type consistency: `_benchmark_flow(adapter, responder, benchmark_id, run_id, arbiter_bin, no_report, notes)` identical in Tasks 1/2; `_report_with_lifecycle(result, arbiter_bin, notes)` stub and implementation match; `FakeArbiterClient.report_benchmark_raw` matches the `_ArbiterClientLike` Protocol used by `report_benchmark_to_arbiter`.
- Task 2 Step 3 offers two finally-forms and defers to the vendored client's actual `stop()` contract — the tests pin observable behavior either way.
