# M3-obs: W3C traceparent in the MCP JSON-RPC Envelope — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every MCP `tools/call` Maestro sends to arbiter carries the active W3C `traceparent` in `params._meta`, so arbiter-side records can be correlated with `benchmark.report.*` / `task.route` spans by `trace_id`.

**Architecture:** One injection point — `ArbiterClient._call_tool_once` — adds `params._meta = {"traceparent": ...}` derived from the active obs span context (`obs.child_env()`). Injection is skipped when there is no real trace context (zero trace-id, e.g. obs not initialized). No wire-contract risk: the pinned arbiter server parses `params` as a raw `serde_json::Value` and reads only `name`/`arguments` (`arbiter-mcp/src/server.rs:458-490`), so `_meta` is ignored until arbiter learns to read it. The arbiter-side reading is a separate repo's work — recorded as a handoff note, not implemented here.

**Tech Stack:** Python 3.12+, vendored-adapted `maestro/coordination/arbiter_client.py` (Maestro-owned; wire contract must stay compatible with pinned arbiter), `maestro/_vendor/obs.py` (pinned, do not modify), pytest (pytest-asyncio auto mode for client tests).

## Global Constraints

- Only `uv`; ruff format/check + `uv run pyrefly check` after changes; line length 88.
- `maestro/_vendor/obs.py` — pinned contract copy, never edit.
- `arbiter_client.py` Don't-list (module docstring): do not touch subprocess lifecycle, reconnect logic, stdio framing, JSON-RPC id sequencing; no imports from `maestro.models`. Adding `_meta` to `tools/call` params touches none of these.
- Real-subprocess proof: the sibling `../arbiter` checkout provides a built pinned binary; `tests/test_arbiter_real_subprocess.py` runs against it locally. Real-subprocess tests deliberately carry NO `@pytest.mark.anyio` (see NOTE in those files; PR #87).
- Branch `feat/m3-obs-traceparent-mcp`; PR-only; human merges.

## File Structure

| File | Role |
|---|---|
| `maestro/coordination/arbiter_client.py` | Modify: `_current_traceparent()` helper + injection in `_call_tool_once` |
| `tests/test_arbiter_client_traceparent.py` | **Create**: unit tests (inject / skip-when-no-context / format) |
| `tests/test_arbiter_real_subprocess.py` | Modify: one e2e test — pinned arbiter tolerates `_meta` |
| `TODO.md` | Modify: close M3-obs items (lines 106, 130), leave arbiter-side follow-up |
| `prograph-vault/authored/notes/` (sibling repo dir) | Handoff note for arbiter-side `_meta.traceparent` reading — written OUTSIDE this repo per polyrepo rules |

---

### Task 1: Injection + unit tests

**Files:**
- Modify: `maestro/coordination/arbiter_client.py` (imports; new helper above `class ArbiterClient`; `_call_tool_once` at `:583`)
- Test: `tests/test_arbiter_client_traceparent.py` (create)

**Interfaces:**
- Consumes: `obs.child_env()` from `maestro/_vendor/obs.py:258` (returns `{"TRACEPARENT": "00-<32hex>-<16hex>-01", ...}`, zero-filled ids when no context); `obs.init_logging`, `obs.span`.
- Produces: `_current_traceparent() -> str | None` (module-level, private); `tools/call` params gain optional `"_meta": {"traceparent": <w3c>}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arbiter_client_traceparent.py`:

```python
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

    (_, params), = capture.calls
    assert params["name"] == "route_task"
    assert params["arguments"] == {"task_id": "t-1"}
    tp = params["_meta"]["traceparent"]
    assert _TRACEPARENT_RE.match(tp)
    assert obs.current_trace_id() in tp


async def test_meta_omitted_without_trace_context(monkeypatch):
    # Fresh contextvars: no init_logging in this test -> zero trace id.
    import structlog

    structlog.contextvars.clear_contextvars()
    client = _make_client()
    capture = _CaptureTransport()
    monkeypatch.setattr(client, "_send_request", capture)

    await client._call_tool_once("get_agent_status", {})

    (_, params), = capture.calls
    assert "_meta" not in params


def test_current_traceparent_none_on_zero_trace():
    import structlog

    structlog.contextvars.clear_contextvars()
    assert _current_traceparent() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_arbiter_client_traceparent.py -v`
Expected: ImportError — `_current_traceparent` does not exist.

- [ ] **Step 3: Implement**

In `maestro/coordination/arbiter_client.py`, add to imports:

```python
from maestro._vendor import obs
```

Add above `class ArbiterClient`:

```python
_ZERO_TRACE_ID = "0" * 32


def _current_traceparent() -> str | None:
    """W3C traceparent of the active obs span context, or None.

    Returns None when there is no real trace context (obs not initialized
    or zero trace-id — the W3C spec treats an all-zero trace-id as invalid),
    so callers can skip injecting `_meta` entirely.
    """
    tp = obs.child_env().get("TRACEPARENT", "")
    parts = tp.split("-")
    if len(parts) != 4 or parts[1] == _ZERO_TRACE_ID:
        return None
    return tp
```

In `_call_tool_once` (`:583`), replace the `_send_request` call:

```python
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        # M3-obs: carry the active W3C trace context in the MCP-sanctioned
        # params._meta slot so arbiter-side records can correlate by
        # trace_id. The pinned arbiter ignores unknown params keys
        # (server parses params as a raw Value and reads only
        # name/arguments), so this is wire-compatible today and becomes
        # useful once arbiter reads it.
        traceparent = _current_traceparent()
        if traceparent is not None:
            params["_meta"] = {"traceparent": traceparent}
        raw = await self._send_request("tools/call", params)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_arbiter_client_traceparent.py tests/test_arbiter_client.py tests/test_arbiter_client_structure.py -q`
Expected: all PASS.

- [ ] **Step 5: Format, typecheck, commit**

```bash
uv run ruff format maestro/coordination/arbiter_client.py tests/test_arbiter_client_traceparent.py
uv run ruff check maestro/coordination/arbiter_client.py --fix && uv run pyrefly check
git add maestro/coordination/arbiter_client.py tests/test_arbiter_client_traceparent.py
git commit -m "feat(obs): inject W3C traceparent into MCP tools/call _meta"
```

---

### Task 2: Real-subprocess tolerance proof

**Files:**
- Modify: `tests/test_arbiter_real_subprocess.py` (append one test; NO anyio marker, matching the file's convention)

- [ ] **Step 1: Append the e2e test**

```python
@real_arbiter_only
async def test_pinned_arbiter_tolerates_meta_traceparent(
    real_arbiter_client, tmp_path, monkeypatch
):
    """M3-obs: params._meta must be ignored by the pinned arbiter build.

    Routes a task with a live obs span so _call_tool_once injects
    _meta.traceparent, and asserts the call still succeeds end-to-end.
    """
    from maestro._vendor import obs

    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    obs.init_logging("maestro")
    with obs.span("task.route", task_id="r05-meta-1"):
        raw = await real_arbiter_client.route_task(
            "r05-meta-1",
            {"type": "bugfix", "language": "python", "description": "meta probe"},
        )
    assert raw.get("assigned_agent") or raw.get("decision") or raw
```

Note: match the existing tests' route_task argument shape — copy the exact
task-spec dict keys used by `test_route_task_response_contains_decision_id_i64`
(`tests/test_arbiter_real_subprocess.py:97`) and assert on the same response
field that test uses.

- [ ] **Step 2: Run the real-subprocess files**

Run: `uv run pytest tests/test_arbiter_real_subprocess.py -q`
Expected: all PASS including the new test (pinned binary ignores `_meta`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_arbiter_real_subprocess.py
git commit -m "test(obs): pinned arbiter tolerates _meta.traceparent end-to-end"
```

---

### Task 3: TODO close-out, handoff, PR

- [ ] **Step 1: TODO.md** — mark line 106 (`M3-obs / arbiter trace`) and line 130 (`M3 — W3C traceparent...`) as `[x]` with date/PR, and note the remaining arbiter-side half:

```markdown
- [x] **M3-obs / arbiter trace** (2026-07-19): W3C `traceparent` инжектится в `params._meta` каждого `tools/call` (`_call_tool_once`); пропуск при нулевом trace-id; e2e-тест — пинованный arbiter игнорирует `_meta`. Arbiter-side чтение `_meta.traceparent` — handoff в prograph-vault/authored/notes/.
```

- [ ] **Step 2: Handoff note** — create `prograph-vault/authored/notes/2026-07-19-arbiter-meta-traceparent-handoff.md` (sibling repo, cross-project note per polyrepo rules) describing: Maestro now sends `params._meta.traceparent` (W3C `00-<trace>-<span>-01`) on every `tools/call`; arbiter should parse it in `server.rs` dispatch and bind trace_id/parent_span_id into its obs layer so `route.decision`/`outcome.recorded`/`benchmark_runs` rows correlate; wire format examples; zero-trace never sent.

- [ ] **Step 3: Full suite + push + PR**

```bash
uv run pytest tests/ -q          # expect 1699+ passed
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git push -u origin feat/m3-obs-traceparent-mcp
gh pr create ...
```

PR body: injection point, `_meta` as the MCP-sanctioned metadata slot, wire-compat proof (server.rs params handling + e2e test), skip-on-zero-trace, arbiter-side follow-up handoff.

## Self-Review

- Wire-compat verified against arbiter source (`server.rs:458-490`: params is a raw Value; only `name`/`arguments` read) AND proven by the e2e test against the pinned binary.
- Out of scope: arbiter-side reading/binding (separate repo, handoff), TRACEPARENT env propagation to the arbiter subprocess (already handled by obs `child_env()` at spawn — this plan adds per-call granularity).
- Type consistency: `_current_traceparent() -> str | None` used identically in Tasks 1 tests and implementation.
