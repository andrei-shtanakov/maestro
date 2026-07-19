"""Unit tests: W3C traceparent injection into MCP tools/call params."""

import re
from typing import Any

from maestro._vendor import obs
from maestro.coordination.arbiter_client import (
    ArbiterClient,
    ArbiterClientConfig,
    _current_traceparent,
)

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


def _make_client() -> ArbiterClient:
    return ArbiterClient(
        ArbiterClientConfig(
            binary_path="/nonexistent/arbiter-mcp",
            tree_path="/nonexistent/tree.json",
            config_dir="/nonexistent/config",
        )
    )


class _CaptureTransport:
    """Monkeypatch target for _send_request: records params, returns a
    canned MCP content envelope."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> dict:
        self.calls.append((method, params))
        return {"content": [{"text": "{}"}]}


async def test_meta_traceparent_injected_inside_span(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    obs.init_logging("maestro")
    client = _make_client()
    capture = _CaptureTransport()
    monkeypatch.setattr(client, "_send_request", capture)

    with obs.span("task.route", task_id="t-1"):
        await client._call_tool_once("route_task", {"task_id": "t-1"})

    ((_, params),) = capture.calls
    assert params["name"] == "route_task"
    assert params["arguments"] == {"task_id": "t-1"}
    tp = params["_meta"]["traceparent"]
    assert _TRACEPARENT_RE.match(tp)
    trace_id = obs.current_trace_id()
    assert trace_id is not None
    assert trace_id in tp


async def test_meta_omitted_without_trace_context(monkeypatch):
    # Fresh contextvars: no init_logging in this test -> zero trace id.
    import structlog

    structlog.contextvars.clear_contextvars()
    client = _make_client()
    capture = _CaptureTransport()
    monkeypatch.setattr(client, "_send_request", capture)

    await client._call_tool_once("get_agent_status", {})

    ((_, params),) = capture.calls
    assert "_meta" not in params


def test_current_traceparent_none_on_zero_trace():
    import structlog

    structlog.contextvars.clear_contextvars()
    assert _current_traceparent() is None
