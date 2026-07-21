"""R-06b M2 — tests for SpawnerResponder.

Mock-only: no real CLI subprocess. We exercise the adapter's three
shapes (happy path / timeout / non-zero exit) with a fake spawner that
writes scripted log content and returns a fake Popen-shaped object.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import pytest

from maestro.benchmark import SpawnerResponder
from maestro.spawners.base import AgentSpawner


if TYPE_CHECKING:
    from pathlib import Path

    from maestro.execution.models import ExecutionRequest
    from maestro.models import Task


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stand-in for ``subprocess.Popen``.

    ``hang_first_wait=True`` blocks ``wait()`` on a threading.Event until
    ``kill()`` is called — exactly the shape our timeout path needs.
    """

    def __init__(self, *, returncode: int = 0, hang_first_wait: bool = False) -> None:
        self._returncode = returncode
        self._hang_first_wait = hang_first_wait
        self._unblock = threading.Event()
        self.killed: bool = False

    @property
    def returncode(self) -> int:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._hang_first_wait and not self._unblock.is_set():
            # Safety net: 5s upper bound so a buggy test doesn't hang CI.
            self._unblock.wait(timeout=5.0)
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self._unblock.set()


class FakeSpawner(AgentSpawner):
    """Real subclass of the ABC so we don't need to mock isinstance checks.

    Writes ``log_content`` into ``log_file`` at spawn time and returns a
    pre-built ``FakeProcess``.
    """

    def __init__(
        self,
        *,
        agent_type_str: str = "claude_code",
        process: FakeProcess | None = None,
        log_content: str = "",
    ) -> None:
        self._agent_type = agent_type_str
        self._process = process if process is not None else FakeProcess()
        self._log_content = log_content
        self.spawn_calls: list[tuple[str, str]] = []

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def is_available(self) -> bool:
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> Any:
        self.spawn_calls.append((task.id, task.prompt))
        log_file.write_text(self._log_content)
        return self._process

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
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_responder_happy_path_parses_tokens_and_cost(tmp_path) -> None:
    """Exit 0 + log with parseable usage → AgentResponse with text=log,
    tokens summed across input+output, cost computed via cost_tracker."""
    log_content = '{"result": "done", "input_tokens": 1500, "output_tokens": 500}'
    spawner = FakeSpawner(log_content=log_content)
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
    )

    response = await responder.respond("write hello world")

    assert response.error is None
    assert response.text == log_content
    assert response.tokens_used == 2000  # 1500 + 500
    assert response.cost_usd is not None
    assert response.cost_usd > 0
    assert spawner.spawn_calls == [("benchmark-1", "write hello world")]
    assert responder.agent_id == "claude_code"


@pytest.mark.anyio
async def test_responder_timeout_kills_process(tmp_path) -> None:
    """Process that doesn't return in time → kill() called, response has
    empty text + error='timeout'. The blocked wait unblocks via kill()."""
    spawner = FakeSpawner(process=FakeProcess(hang_first_wait=True))
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=0.05,
    )

    response = await responder.respond("hangs forever")

    assert response.error == "timeout"
    assert response.text == ""
    assert response.tokens_used is None
    assert response.cost_usd is None
    assert spawner._process.killed is True


@pytest.mark.anyio
async def test_responder_nonzero_exit_reports_error(tmp_path) -> None:
    """Non-zero exit → text empty, error='exit code N'. Tokens/cost are
    still parsed from whatever log was captured before the crash (may be
    None when the agent didn't get far enough to emit usage)."""
    spawner = FakeSpawner(
        process=FakeProcess(returncode=2),
        log_content="",  # crash before usage was emitted
    )
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
    )

    response = await responder.respond("breaks")

    assert response.error == "exit code 2"
    assert response.text == ""
    assert response.tokens_used is None
    assert response.cost_usd is None


@pytest.mark.anyio
async def test_reported_cost_preferred_over_pricing(tmp_path) -> None:
    """When the parsed usage carries agent-reported cost, the responder
    forwards it instead of the PRICING estimate (0.0 for opencode)."""
    log_content = (
        '{"type": "step_finish", "part": {"cost": 0.02, "tokens": '
        '{"input": 100, "output": 20, "reasoning": 0}}}'
    )
    spawner = FakeSpawner(agent_type_str="opencode", log_content=log_content)
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
    )

    response = await responder.respond("write hello world")

    assert response.error is None
    assert response.cost_usd == pytest.approx(0.02)
    assert response.tokens_used == 120


@pytest.mark.anyio
async def test_reported_zero_cost_is_preserved(tmp_path) -> None:
    """A genuinely-reported 0.0 (e.g. a free model) must survive to the
    wire format — it is an observation, not an absence of one."""
    log_content = json.dumps(
        {
            "type": "step_finish",
            "part": {"cost": 0.0, "tokens": {"input": 10, "output": 5}},
        }
    )
    spawner = FakeSpawner(agent_type_str="opencode", log_content=log_content)
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
    )

    response = await responder.respond("free run")

    assert response.error is None
    assert response.cost_usd == 0.0  # reported 0.0, NOT collapsed to None


@pytest.mark.anyio
async def test_reported_zero_cost_preserved_on_error_exit(tmp_path) -> None:
    """A reported 0.0 must survive even on a non-zero exit — both return
    sites share the same cost_wire, so the error path cannot re-collapse it."""
    log_content = json.dumps(
        {
            "type": "step_finish",
            "part": {"cost": 0.0, "tokens": {"input": 10, "output": 5}},
        }
    )
    spawner = FakeSpawner(
        agent_type_str="opencode",
        process=FakeProcess(returncode=2),
        log_content=log_content,
    )
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
    )

    response = await responder.respond("free but failed")

    assert response.cost_usd == 0.0  # reported 0.0 preserved on error path
    assert response.error == "exit code 2"  # error is still reported


@pytest.mark.anyio
async def test_estimated_zero_cost_stays_unknown(tmp_path) -> None:
    """No parseable usage → no reported cost, zero tokens → estimate 0.0
    → collapses to None (an estimate of zero is not an observation)."""
    spawner = FakeSpawner(agent_type_str="claude_code", log_content="{}")
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
    )

    response = await responder.respond("no usage")

    assert response.cost_usd is None


@pytest.mark.anyio
async def test_responder_unknown_agent_type_short_circuits(tmp_path) -> None:
    """If a wrapped spawner reports an agent_type not in AgentType enum,
    the adapter must surface the error rather than crash mid-spawn."""
    spawner = FakeSpawner(agent_type_str="not_a_real_agent")
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
    )

    response = await responder.respond("doesn't matter")

    assert response.error is not None
    assert "unknown agent_type" in response.error
    assert spawner.spawn_calls == []  # never reached spawn
