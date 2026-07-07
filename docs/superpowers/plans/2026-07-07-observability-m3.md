# Observability M3 — runtime-decision instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit a `scheduler.tick` record per poll cycle (on change only) and wrap the per-task routing decision in a `task.route` span, so queue evolution and routing latency/outcome are observable.

**Architecture:** Two small extracted methods on `Scheduler`: `_emit_tick` (emit-on-change via the module `_obs_log`) called from `_main_loop`, and `_route_task` (an `obs.span("task.route", …)` wrap of `self._routing.route`) called from `_spawn_task`. Consistent with M1/M2's obs primitives (structured emit + `obs.span`); no metrics backend.

**Tech Stack:** Python 3.12+, uv, structlog-based `maestro._vendor.obs`, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-07-observability-m3-runtime-decision-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`; run pytest in the FOREGROUND.
- `scheduler.tick`: the FIRST tick always emits; subsequent ticks emit ONLY when the `(ready, running, completed)` snapshot changes (`self._last_tick`, init `None`). NOT a fixed-interval heartbeat.
- `task.route`: `obs.span` emits `task.route.started` (attrs: `task_id` only) on entry, `task.route.ended` (attrs incl. `action`/`chosen_agent`/`decision_id`) on success — the decision attrs land on `.ended`, NOT `.started`. On a routing exception `obs.span` auto-emits `task.route.failed` (with `error=`) and re-raises — no extra handling; the scheduler's existing exception flow is unchanged.
- `decision_id` is `None` for a static route — expected, asserted in tests.
- Emit records auto-carry `trace_id`/`span_id` from the surrounding `scheduler.session` span (obs contextvars) — no extra correlation code.
- Non-goals (do NOT implement): observability dashboards; W3C `traceparent` injection into the MCP JSON-RPC envelope; a per-second heartbeat.
- Test harness: the scheduler binds `_obs_log` at import, so after `importlib.reload(obs)` + `init_logging` pointed at a tmp `ORCHESTRA_LOG_DIR`, ALSO `monkeypatch.setattr(scheduler_module, "_obs_log", obs.get_logger("maestro.scheduler"))`; `obs.span` uses the reloaded module directly. Mirror `tests/test_scheduler_observability.py`.
- Branch: `feat/observability-m3` (exists, spec committed). Full suite green at every commit.

---

### Task 1: `scheduler.tick` emit-on-change

**Files:**
- Modify: `maestro/scheduler.py` (`__init__` add `_last_tick`; new `_emit_tick`; call in `_main_loop`)
- Test: `tests/test_scheduler_observability_m3.py` (new)

**Interfaces:**
- Produces: `Scheduler._emit_tick(self, ready: int, running: int, completed: int) -> None`; `Scheduler._last_tick: tuple[int, int, int] | None`.

- [ ] **Step 1: Write the failing test (new file with the reload harness)**

Create `tests/test_scheduler_observability_m3.py`:

```python
"""Observability M3 — scheduler.tick (emit-on-change) and task.route span.

Mirrors tests/test_scheduler_observability.py: the scheduler binds `_obs_log`
at import, so we reload obs into a tmp ORCHESTRA_LOG_DIR and rebind the
scheduler module's `_obs_log` to a fresh logger; `obs.span` uses the reloaded
module directly.
"""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING

import pytest

from maestro.dag import DAG
from maestro.models import RouteAction, RouteDecision

# NOTE: SchedulerConfig lives in maestro.scheduler (not maestro.models); the
# tests build it from the reloaded module as sched_mod.SchedulerConfig(...).

if TYPE_CHECKING:
    from pathlib import Path


def _reload_obs_and_scheduler(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import maestro._vendor.obs as obs

    importlib.reload(obs)
    obs.init_logging("maestro")
    import maestro.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_obs_log", obs.get_logger("maestro.scheduler"))
    return obs, sched_mod


def _read_records(tmp_path: Path) -> list[dict]:
    files = list(tmp_path.glob("maestro-*.jsonl"))
    assert len(files) == 1, f"expected 1 jsonl file, got {len(files)}: {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def _make_scheduler(sched_mod, tmp_path):
    return sched_mod.Scheduler(
        db=object(),  # _emit_tick / _route_task never touch db
        dag=DAG([]),
        spawners={},
        config=sched_mod.SchedulerConfig(
            workdir=tmp_path, log_dir=tmp_path / "logs"
        ),
    )


def _ticks(tmp_path: Path) -> list[dict]:
    return [
        r
        for r in _read_records(tmp_path)
        if r["Attributes"].get("event") == "scheduler.tick"
    ]


def test_emit_tick_first_and_on_change_only(tmp_path, monkeypatch) -> None:
    obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)

    sched._emit_tick(2, 1, 0)  # first → emits
    sched._emit_tick(2, 1, 0)  # identical → skipped
    sched._emit_tick(1, 2, 0)  # changed → emits

    ticks = _ticks(tmp_path)
    assert len(ticks) == 2
    assert ticks[0]["Attributes"]["ready"] == 2
    assert ticks[0]["Attributes"]["running"] == 1
    assert ticks[0]["Attributes"]["completed"] == 0
    assert ticks[1]["Attributes"]["ready"] == 1
    assert ticks[1]["Attributes"]["running"] == 2
    assert ticks[0]["Resource"]["service.name"] == "maestro"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_scheduler_observability_m3.py -k emit_tick -q`
Expected: FAIL — `Scheduler` has no `_emit_tick`.

- [ ] **Step 3: Add `_last_tick` to `__init__`**

In `maestro/scheduler.py` `Scheduler.__init__`, next to the other runtime
state (after `self._running_tasks: dict[str, RunningTask] = {}`):

```python
        self._last_tick: tuple[int, int, int] | None = None
```

- [ ] **Step 4: Add the `_emit_tick` method**

Add to `Scheduler` (near `_main_loop`):

```python
    def _emit_tick(self, ready: int, running: int, completed: int) -> None:
        """Emit a per-poll-cycle queue snapshot, but only when it changed.

        The first tick always emits; identical consecutive snapshots are
        skipped so a long idle run does not flood with duplicate ticks.
        """
        snapshot = (ready, running, completed)
        if snapshot == self._last_tick:
            return
        self._last_tick = snapshot
        _obs_log.info(
            "scheduler.tick", ready=ready, running=running, completed=completed
        )
```

- [ ] **Step 5: Call it in `_main_loop`**

In `_main_loop`, after `await self._monitor_running_tasks()` and before the
poll-interval wait, add:

```python
            # M3: per-poll-cycle queue snapshot (emit-on-change).
            self._emit_tick(
                len(ready_task_ids), len(self._running_tasks), len(completed_ids)
            )
```

- [ ] **Step 6: Run the test + gates**

Run: `uv run pytest tests/test_scheduler_observability_m3.py -k emit_tick -q`
Then: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; clean.

- [ ] **Step 7: Commit**

```bash
git add maestro/scheduler.py tests/test_scheduler_observability_m3.py
git commit -m "feat(obs): scheduler.tick emit-on-change per poll cycle (M3)"
```

---

### Task 2: `task.route` span

**Files:**
- Modify: `maestro/scheduler.py` (new `_route_task`; call it in `_spawn_task`)
- Test: `tests/test_scheduler_observability_m3.py` (extend)

**Interfaces:**
- Consumes: `self._routing.route(task)` (existing), `obs.span`, `RouteDecision`.
- Produces: `Scheduler._route_task(self, task: Task) -> RouteDecision`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler_observability_m3.py`:

```python
class _StubRouting:
    def __init__(self, decision=None, exc=None):
        self._decision = decision
        self._exc = exc

    async def route(self, task):
        if self._exc is not None:
            raise self._exc
        return self._decision

    async def report_outcome(self, task, outcome):  # unused here
        return None


def _make_task():
    from maestro.models import Task

    # Task required fields: id, title, prompt, workdir.
    return Task(id="t-1", title="t-1", prompt="p", workdir="/tmp")


def _events(tmp_path, name):
    return [
        r for r in _read_records(tmp_path) if r["Attributes"].get("event") == name
    ]


@pytest.mark.anyio
async def test_route_span_ended_carries_decision_attrs(tmp_path, monkeypatch) -> None:
    obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._routing = _StubRouting(
        decision=RouteDecision(action=RouteAction.ASSIGN, decision_id=None, reason="ok")
    )

    decision = await sched._route_task(_make_task())
    assert decision.decision_id is None  # static route → None is expected

    started = _events(tmp_path, "task.route.started")
    ended = _events(tmp_path, "task.route.ended")
    assert len(started) == 1 and len(ended) == 1
    # started carries only task_id; the decision attrs are on ended.
    assert started[0]["Attributes"]["task_id"] == "t-1"
    assert "action" not in started[0]["Attributes"]
    assert ended[0]["Attributes"]["action"] == "assign"
    assert ended[0]["Attributes"]["decision_id"] is None


@pytest.mark.anyio
async def test_route_span_records_arbiter_decision_id(tmp_path, monkeypatch) -> None:
    obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._routing = _StubRouting(
        decision=RouteDecision(
            action=RouteAction.ASSIGN, decision_id="d-123", reason="ok"
        )
    )
    await sched._route_task(_make_task())
    ended = _events(tmp_path, "task.route.ended")
    assert ended[0]["Attributes"]["decision_id"] == "d-123"


@pytest.mark.anyio
async def test_route_span_failed_on_exception(tmp_path, monkeypatch) -> None:
    obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._routing = _StubRouting(exc=RuntimeError("arbiter down"))

    with pytest.raises(RuntimeError):
        await sched._route_task(_make_task())

    failed = _events(tmp_path, "task.route.failed")
    assert len(failed) == 1
    assert "error" in failed[0]["Attributes"]
```

(Adjust `_make_task()` to the real `Task` required fields — check
`maestro/models.py`'s `Task`; the stub only needs a valid `Task` with an `id`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scheduler_observability_m3.py -k route_span -q`
Expected: FAIL — `Scheduler` has no `_route_task`.

- [ ] **Step 3: Add the `_route_task` method**

Add to `Scheduler`:

```python
    async def _route_task(self, task: Task) -> RouteDecision:
        """Consult the routing strategy inside a `task.route` span.

        The span records the decision (action / chosen_agent / decision_id) on
        its `.ended` record; a routing exception surfaces as `task.route.failed`
        (obs.span re-raises, preserving the caller's error flow).
        """
        with obs.span("task.route", task_id=task.id) as route_span:
            decision = await self._routing.route(task)
            route_span.set_attrs(
                action=decision.action.value,
                chosen_agent=decision.chosen_agent,
                decision_id=decision.decision_id,
            )
            return decision
```

- [ ] **Step 4: Call it from `_spawn_task`**

In `_spawn_task` (maestro/scheduler.py), replace the direct routing call:

```python
        # R-03: consult the routing strategy before picking a spawner.
        decision = await self._routing.route(task)
```

with:

```python
        # R-03: consult the routing strategy before picking a spawner.
        # M3: wrapped in a task.route span (latency + decision outcome).
        decision = await self._route_task(task)
```

- [ ] **Step 5: Run tests + gates**

Run: `uv run pytest tests/test_scheduler_observability_m3.py -q`
Then: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; full suite green (existing scheduler + M2 obs tests unchanged); clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/scheduler.py tests/test_scheduler_observability_m3.py
git commit -m "feat(obs): task.route span around the routing decision (M3)"
```

---

### Task 3: docs, TODO tick, final gates, PR

**Files:**
- Modify: `TODO.md`

- [ ] **Step 1: TODO.md — tick the M3 sub-items**

Update the Observability M3 line (currently `- [ ] **M3** (pending):
scheduler-tick instrumentation …, arbiter routing decision span, observability
dashboards`) to record the two shipped slices and leave dashboards +
traceparent open:

```markdown
- [x] **M3 (runtime-decision instrumentation)** (closed by feat/observability-m3): `scheduler.tick` emit-on-change per poll cycle + `task.route` span around the routing decision (covers static + arbiter, records latency/decision_id; failure → `task.route.failed`).
- [ ] **M3 — observability dashboards** (pending): separate project (backend/viz over the OTel JSONL or the existing `maestro/dashboard/` UI).
- [ ] **M3 — W3C traceparent into the MCP JSON-RPC envelope** (the R-06b arbiter-trace follow-up): cross-boundary contract; correlates `benchmark.report.*` / routing with arbiter-side rows by trace_id.
```

- [ ] **Step 2: Final gates**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
```

Expected: full suite green; pyrefly 0; ruff clean.

- [ ] **Step 3: Commit docs**

```bash
git add TODO.md
git commit -m "docs: observability M3 runtime-decision instrumentation shipped"
```

- [ ] **Step 4: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/observability-m3
gh pr create --title "feat(obs): observability M3 runtime-decision instrumentation" --body "$(cat <<'EOF'
## Summary
- `scheduler.tick`: emit a per-poll-cycle queue snapshot (`ready` / `running` / `completed`) from `_main_loop`, **emit-on-change** — the first tick always emits, then only when the snapshot changes, so a long idle run doesn't flood with duplicate ticks. Not a fixed-interval heartbeat (liveness is covered by M2's `scheduler.session` span + task-lifecycle emits)
- `task.route`: wrap the per-task routing decision (`self._routing.route`) in an `obs.span`, covering both `StaticRouting` and `ArbiterRoutingStrategy` — records the arbiter call's latency and the decision (`action` / `chosen_agent` / `decision_id`). The decision attrs land on `task.route.ended`; a routing exception surfaces as `task.route.failed` (obs.span re-raises, so the scheduler's error flow is unchanged). `decision_id` is `None` for a static route (expected)
- Both emit records auto-carry `trace_id`/`span_id` from the surrounding `scheduler.session` span

## Non-goals (separate tickets)
- Observability dashboards
- W3C `traceparent` injection into the MCP JSON-RPC envelope (arbiter-trace follow-up)
- Per-second heartbeat (this is emit-on-change)

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] `_emit_tick`: first emits, identical snapshot skipped, changed snapshot emits (discriminating)
- [ ] `task.route`: decision attrs on `.ended` not `.started`; `decision_id` None (static) and set (arbiter-like); a raising strategy → `task.route.failed` + propagates
- [ ] existing M2 `test_scheduler_observability.py` unchanged

Observability M3 slice (tick + routing span); dashboards + traceparent deferred.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: scheduler.tick emit-on-change → Task 1; task.route span (start/end/failed, decision attrs on ended, static decision_id None) → Task 2; non-goals untouched; docs/TODO → Task 3.
- Type consistency: `_emit_tick(ready, running, completed) -> None`; `_last_tick: tuple[int,int,int]|None`; `_route_task(task) -> RouteDecision`. Consistent across tasks.
- The reload+`_obs_log`-monkeypatch harness is the load-bearing test setup (scheduler binds `_obs_log` at import); stated in Global Constraints and Task 1 Step 1.
- The implementer must confirm `Task`'s required fields for `_make_task()` (Task 2 Step 1 note) — the stub only needs a valid Task with an `id`.
- Three tasks: Task 1 (tick), Task 2 (route span), Task 3 (docs+PR) — each an independent reviewer gate.
