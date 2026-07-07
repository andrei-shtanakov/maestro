# Observability M3 — runtime-decision instrumentation — design

**Date:** 2026-07-07
**Status:** approved
**Context:** Cross-project Observability milestone M3 (TODO). M1 (trace
continuity) and M2 (scheduler session + task.spawn spans, 4 task-lifecycle
emits) are done. M3 as listed bundles three items of very different size —
scheduler-tick instrumentation, an arbiter routing-decision span, and
observability dashboards. This spec takes the two small, cohesive
runtime-decision instrumentation slices; dashboards and traceparent injection
are separate tickets (see Non-goals).

## Problem

The scheduler's per-poll-cycle state and its per-task routing decision are not
observable. M2 instruments the run boundary (`scheduler.session`) and the spawn
(`task.spawn`) plus terminal task events, but there is no signal for:

1. **Queue evolution over time** — how ready / running / completed counts change
   across poll cycles. A stuck or slowly-draining queue is invisible between
   task-level events.
2. **Routing latency and outcome** — `self._routing.route(task)` (scheduler.py,
   the spawn path) makes the routing decision (a network call for the arbiter
   strategy) with no span, so its latency and the decision it returned are not
   traced.

Both are the same layer — runtime decision instrumentation — and both use the
existing obs primitives (structured emit + `obs.span`), consistent with M1/M2.
There is no metrics backend; "metrics" here means structured emit records.

## Change

### 1. `scheduler.tick` — emit-on-change per poll cycle

Add `Scheduler._emit_tick(ready: int, running: int, completed: int) -> None`,
called once per `_main_loop` iteration after `_monitor_running_tasks()` (the
freshest running count for the tick). It emits via the module `_obs_log`:

```python
    def _emit_tick(self, ready: int, running: int, completed: int) -> None:
        snapshot = (ready, running, completed)
        if snapshot == self._last_tick:
            return
        self._last_tick = snapshot
        _obs_log.info(
            "scheduler.tick", ready=ready, running=running, completed=completed
        )
```

- `self._last_tick: tuple[int, int, int] | None` is initialised to `None` in
  `__init__`, so the **first tick always emits**; subsequent ticks emit **only
  when the (ready, running, completed) snapshot changes**.
- Rationale: without emit-on-change a long idle run would emit hundreds of
  identical ticks (poll_interval defaults to 1s). The value of per-cycle metrics
  is the queue's *evolution* — each change is a data point; identical repeats add
  nothing. Scheduler liveness between changes is already covered by M2's
  `scheduler.session` span and the task-lifecycle emits.
- Call site in `_main_loop`: after `_monitor_running_tasks()`, before the
  poll-interval wait —
  `self._emit_tick(len(ready_task_ids), len(self._running_tasks), len(completed_ids))`.
  (`completed_ids` and `ready_task_ids` are already computed in the loop body;
  `self._running_tasks` is current after monitoring.)
- The emit auto-carries `trace_id` / `span_id` from the surrounding
  `scheduler.session` span via obs contextvars — no extra correlation work.

### 2. `task.route` span around the routing decision

Wrap the routing call in the spawn path (scheduler.py, where
`decision = await self._routing.route(task)`):

```python
        with obs.span("task.route", task_id=task_id) as route_span:
            decision = await self._routing.route(task)
            route_span.set_attrs(
                action=decision.action.value,
                chosen_agent=decision.chosen_agent,
                decision_id=decision.decision_id,
            )
```

Covers BOTH strategies uniformly: `StaticRouting` (fast; `action="assign"`,
`decision_id=None`) and `ArbiterRoutingStrategy` (records the arbiter network
call's latency + the returned `decision_id`). `RouteDecision` fields used:
`action` (RouteAction enum → `.value`), `chosen_agent: str | None`,
`decision_id: str | None`.

**obs.span record shape (matters for the tests — no false expectations):**
`obs.span` emits `task.route.started` on entry carrying only the initial attrs
(`task_id`), then on success `task.route.ended` carrying the `set_attrs`
attributes (`action` / `chosen_agent` / `decision_id`). So the decision
attributes land on the **`.ended`** record, NOT `.started`.

**Failure:** if `self._routing.route(task)` raises, `obs.span` automatically
emits `task.route.failed` with `error=<exc dict>` (plus any attrs already set —
here `set_attrs` runs only after a successful `route()`, so the failed record
carries `task_id` + the error) and **re-raises**. This is built into `obs.span`;
no extra handling is added, and the scheduler's existing exception flow is
unchanged. A raising routing strategy is thus observable as a `task.route.failed`
record.

## Testing

Mirror `tests/test_scheduler_observability.py`'s harness (reload
`maestro._vendor.obs` into a tmp `ORCHESTRA_LOG_DIR`, run the instrumented code,
read the OTel-shaped `maestro-*.jsonl`, assert on `Attributes.event`).

- **`_emit_tick` emit-on-change (discriminating):** first call emits a
  `scheduler.tick` record; a second call with the SAME (ready, running,
  completed) emits nothing (still one record); a third call with a CHANGED
  snapshot emits a second record. Assert the record's `Attributes.event ==
  "scheduler.tick"` and the `ready` / `running` / `completed` attributes.
- **`task.route` span on success:** drive the routing wrap with a stub strategy
  returning `action=ASSIGN, decision_id=None` (static-like) → assert a
  `task.route.ended` record whose attrs include `action == "assign"` and
  `decision_id is None`; and one returning `decision_id="d-123"` (arbiter-like) →
  `.ended` record with `decision_id == "d-123"`. Assert the decision attrs are on
  `.ended`, not `.started` (per the record-shape note). `decision_id=None` for a
  static route is expected and asserted.
- **`task.route` failure:** a stub strategy whose `route()` raises → a
  `task.route.failed` record with an `error` attribute is emitted AND the
  exception propagates (`pytest.raises`).
- Regression: M2's `scheduler.session` / `task.spawn` / task-lifecycle emits and
  the existing `test_scheduler_observability.py` cases stay green.

## Non-goals (explicit)

- **Observability dashboards** — out of scope; a separate, larger project (its
  own design: backend / visualization, over the existing `maestro/dashboard/` UI
  or an external stack over the OTel JSONL).
- **W3C `traceparent` injection into the MCP JSON-RPC envelope** (the R-06b
  M3-obs / arbiter-trace follow-up, TODO) — out of scope; that is a cross-boundary
  contract change (arbiter must read it). This spec's `task.route` span is
  Maestro-side only.
- **Per-second heartbeat** — out of scope; `scheduler.tick` is emit-on-change,
  NOT a fixed-interval heartbeat. Liveness is covered by the `scheduler.session`
  span and task-lifecycle emits.

## Documentation

- TODO.md: tick the Observability M3 line's tick + routing-span sub-items;
  leave dashboards + traceparent as their own open items.
- The observability contract in `../_cowork_output/observability-contract/`
  documents event names — add `scheduler.tick` and `task.route.{started,ended,
  failed}` if the contract enumerates events (check; update only if it does).
