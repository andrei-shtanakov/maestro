"""R-06b M3 — adapter tests against a fake ATP HTTP layer.

Tests monkeypatch ``AsyncATPClient._request`` (the single chokepoint for
HTTP) and feed canned ``httpx.Response`` objects in. That exercises the
adapter's translation of M1 Protocols → ATP HTTP API without spinning up
a real ATP server.

Live-server integration tests are intentionally out of scope here; gate
those behind ``MAESTRO_ATP_BASE_URL`` if/when needed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from atp_sdk.client import AsyncATPClient

from maestro.benchmark import (
    AgentResponse,
    BenchmarkResult,
    BenchmarkRunner,
    MaestroATPAdapter,
)


def _response(status_code: int, payload: Any) -> httpx.Response:
    """Build an ``httpx.Response`` with a JSON body."""
    request = httpx.Request("GET", "http://test.local/")
    if status_code == 204:
        return httpx.Response(status_code=204, request=request)
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        request=request,
    )


def _atp_request(task_index: int, *, task_id: str, prompt: str) -> dict[str, Any]:
    """Build an ATPRequest dict shaped like ``benchmark_api.next_task``."""
    return {
        "version": "1.0",
        "task_id": task_id,
        "task": {
            "description": prompt,
            "input_data": {},
            "expected_artifacts": [],
        },
        "constraints": {},
        "metadata": {
            "task_index": task_index,
            "test_id": f"t-{task_index}",
            "test_name": f"Task {task_index}",
            "run_id": 42,
        },
    }


class FakeRequestQueue:
    """Replays a queued list of ``(method, url_substring) → httpx.Response``.

    We match by method + url substring so tests stay readable when the
    full URL contains run/benchmark IDs. Each call records its arguments
    for downstream assertions and pops the matching response.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._queue: list[tuple[str, str, httpx.Response]] = []

    def enqueue(
        self, method: str, url_substring: str, response: httpx.Response
    ) -> None:
        self._queue.append((method, url_substring, response))

    async def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        for i, (m, sub, resp) in enumerate(self._queue):
            if m == method and sub in url:
                self._queue.pop(i)
                return resp
        raise AssertionError(
            f"Unexpected request {method} {url}; "
            f"queued: {[(m, sub) for m, sub, _ in self._queue]}"
        )


@pytest.fixture
def fake_atp(monkeypatch: pytest.MonkeyPatch) -> FakeRequestQueue:
    """Return a queue and patch ``AsyncATPClient._request`` to drain it."""
    queue = FakeRequestQueue()
    monkeypatch.setattr(AsyncATPClient, "_request", queue)
    return queue


@pytest.mark.anyio
async def test_from_token_sets_authorization_header() -> None:
    """``from_token`` constructs an ``AsyncATPClient`` with a Bearer header
    so requests authenticate even when ``ATP_TOKEN`` is unset."""
    adapter = MaestroATPAdapter.from_token(
        "secret-123", platform_url="http://atp.local"
    )
    try:
        client: AsyncATPClient = adapter._client
        assert client.token == "secret-123"
        assert client._http.headers.get("Authorization") == "Bearer secret-123"
    finally:
        await adapter.close()


@pytest.mark.anyio
async def test_from_env_falls_back_to_atp_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``from_env`` should pick up ``ATP_TOKEN`` via the SDK's default
    resolution chain when no explicit token is passed."""
    monkeypatch.setenv("ATP_TOKEN", "env-token")
    adapter = MaestroATPAdapter.from_env(platform_url="http://atp.local")
    try:
        assert adapter._client.token == "env-token"
    finally:
        await adapter.close()


@pytest.mark.anyio
async def test_start_run_returns_adapter_with_string_run_id(
    fake_atp: FakeRequestQueue,
) -> None:
    """ATP returns ``run_id`` as ``int``; adapter must expose ``str``
    so it satisfies M1's ``BenchmarkRun.run_id: str``."""
    fake_atp.enqueue(
        "POST",
        "/api/v1/benchmarks/swe-mini/start",
        _response(200, {"id": 42}),
    )
    adapter = MaestroATPAdapter.from_token("t", platform_url="http://atp.local")
    try:
        run = await adapter.start_run("swe-mini", "claude_code")
        assert run.run_id == "42"  # int → str
    finally:
        await adapter.close()


@pytest.mark.anyio
async def test_run_adapter_iterates_and_submits_with_artifacts(
    fake_atp: FakeRequestQueue,
) -> None:
    """End-to-end: adapter pulls 2 tasks, submits responses with the
    agent text wrapped in a structured artifact, and reuses the original
    ATPRequest task_id when building each ATPResponse."""
    fake_atp.enqueue(
        "POST",
        "/api/v1/benchmarks/swe-mini/start",
        _response(200, {"id": 7}),
    )
    fake_atp.enqueue(
        "GET",
        "/api/v1/runs/7/next-task",
        _response(200, _atp_request(0, task_id="uuid-A", prompt="fix X")),
    )
    fake_atp.enqueue(
        "POST",
        "/api/v1/runs/7/submit",
        _response(200, {"task_index": 0, "score": 100.0}),
    )
    fake_atp.enqueue(
        "GET",
        "/api/v1/runs/7/next-task",
        _response(200, _atp_request(1, task_id="uuid-B", prompt="fix Y")),
    )
    fake_atp.enqueue(
        "POST",
        "/api/v1/runs/7/submit",
        _response(200, {"task_index": 1, "score": 100.0}),
    )
    fake_atp.enqueue("GET", "/api/v1/runs/7/next-task", _response(204, None))
    fake_atp.enqueue(
        "GET",
        "/api/v1/runs/7/status",
        _response(200, {"id": 7, "total_score": 92.5, "tasks_count": 2}),
    )

    class EchoResponder:
        agent_id = "claude_code"

        async def respond(self, prompt: str) -> AgentResponse:
            return AgentResponse(
                text=f"answer to {prompt}", tokens_used=5, cost_usd=0.0001
            )

    adapter = MaestroATPAdapter.from_token("t", platform_url="http://atp.local")
    try:
        runner = BenchmarkRunner(adapter, EchoResponder())
        result = await runner.run(benchmark_id="swe-mini")
    finally:
        await adapter.close()

    assert isinstance(result, BenchmarkResult)
    assert result.run_id == "7"
    assert result.score == 92.5
    assert result.score_components == {}
    assert len(result.per_task) == 2
    assert result.per_task[0].response == "answer to fix X"
    assert result.total_tokens == 10

    # Verify the submit calls carry the agent text inside an artifact and
    # reuse the task_id from each ATPRequest.
    submit_calls = [c for c in fake_atp.calls if c[1].endswith("/submit")]
    assert len(submit_calls) == 2

    payload_0 = submit_calls[0][2]["json"]
    assert payload_0["task_index"] == 0
    assert payload_0["response"]["task_id"] == "uuid-A"
    assert payload_0["response"]["status"] == "completed"
    assert payload_0["response"]["artifacts"][0]["data"] == {"text": "answer to fix X"}

    payload_1 = submit_calls[1][2]["json"]
    assert payload_1["response"]["task_id"] == "uuid-B"


@pytest.mark.anyio
async def test_submit_marks_failed_when_response_empty(
    fake_atp: FakeRequestQueue,
) -> None:
    """An empty agent response (e.g. timeout) submits with
    ``status="failed"`` and no artifact — preserving M1's policy that the
    runner does not pre-judge no-answer, ATP scoring decides."""
    fake_atp.enqueue(
        "POST",
        "/api/v1/benchmarks/b/start",
        _response(200, {"id": 1}),
    )
    fake_atp.enqueue(
        "GET",
        "/api/v1/runs/1/next-task",
        _response(200, _atp_request(0, task_id="uuid-T", prompt="x")),
    )
    fake_atp.enqueue(
        "POST",
        "/api/v1/runs/1/submit",
        _response(200, {"task_index": 0, "score": 0.0}),
    )
    fake_atp.enqueue("GET", "/api/v1/runs/1/next-task", _response(204, None))
    fake_atp.enqueue(
        "GET",
        "/api/v1/runs/1/status",
        _response(200, {"id": 1, "total_score": 0.0}),
    )

    class FailingResponder:
        agent_id = "codex_cli"

        async def respond(self, prompt: str) -> AgentResponse:
            return AgentResponse(text="", error="timeout")

    adapter = MaestroATPAdapter.from_token("t", platform_url="http://atp.local")
    try:
        runner = BenchmarkRunner(adapter, FailingResponder())
        result = await runner.run(benchmark_id="b")
    finally:
        await adapter.close()

    submit_call = next(c for c in fake_atp.calls if c[1].endswith("/submit"))
    payload = submit_call[2]["json"]
    assert payload["response"]["status"] == "failed"
    assert "artifacts" not in payload["response"]
    assert result.per_task[0].error == "timeout"


@pytest.mark.anyio
async def test_finalize_returns_zero_when_total_score_missing(
    fake_atp: FakeRequestQueue,
) -> None:
    """If ATP's ``/status`` omits ``total_score`` (e.g. run still
    in-progress when finalize is called), surface it as 0.0 rather than
    crashing — the BenchmarkResult contract requires a numeric score."""
    fake_atp.enqueue(
        "POST",
        "/api/v1/benchmarks/b/start",
        _response(200, {"id": 99}),
    )
    fake_atp.enqueue("GET", "/api/v1/runs/99/next-task", _response(204, None))
    fake_atp.enqueue(
        "GET",
        "/api/v1/runs/99/status",
        _response(200, {"id": 99, "status": "in_progress"}),
    )

    class NoopResponder:
        agent_id = "x"

        async def respond(self, prompt: str) -> AgentResponse:  # pragma: no cover
            return AgentResponse(text="")

    adapter = MaestroATPAdapter.from_token("t", platform_url="http://atp.local")
    try:
        runner = BenchmarkRunner(adapter, NoopResponder())
        result = await runner.run(benchmark_id="b")
    finally:
        await adapter.close()

    assert result.score == 0.0
    assert result.per_task == []


def test_extract_task_type_from_metadata() -> None:
    """Pull task_type out of ATP metadata when present."""
    from maestro.benchmark.atp_client import _extract_task_type

    raw = {
        "task_id": "t0",
        "task": {"description": "fix"},
        "metadata": {"task_index": 0, "task_type": "bugfix"},
    }
    assert _extract_task_type(raw) == "bugfix"


def test_extract_task_type_none_when_absent() -> None:
    """Return None when task_type is not in metadata."""
    from maestro.benchmark.atp_client import _extract_task_type

    raw = {
        "task_id": "t0",
        "task": {"description": "fix"},
        "metadata": {"task_index": 0},
    }
    assert _extract_task_type(raw) is None


def test_extract_task_type_none_when_metadata_missing() -> None:
    """Return None when metadata is missing entirely."""
    from maestro.benchmark.atp_client import _extract_task_type

    raw = {"task_id": "t0", "task": {"description": "fix"}}
    assert _extract_task_type(raw) is None
