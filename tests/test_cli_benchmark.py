"""Tests for `maestro benchmark` (R-06b M5)."""

from pathlib import Path
from typing import ClassVar

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

    instances: ClassVar[list["FakeAdapter"]] = []

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

    def spawn(
        self, task: Task, context, workdir, log_file, retry_context="", *, model=None
    ):
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
        assert "prompt" in result.output
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


class FakeArbiterClient:
    """Records lifecycle calls; behavior configured by class attrs."""

    instances: ClassVar[list["FakeArbiterClient"]] = []
    fail_start: ClassVar[bool] = False

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
