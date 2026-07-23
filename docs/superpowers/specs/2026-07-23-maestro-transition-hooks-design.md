# Transition hooks — design

- **Date:** 2026-07-23
- **Status:** approved-in-review (brainstorming; architectural vector confirmed, corrections folded in)
- **Scope:** idea #10 from `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`
  ("unified Phase enum + phase-transition hooks") — the **transition-hooks / anti-desync** half.

## 1. Problem

Status-change side effects are wired by hand at each transition site, inconsistently:

- **Scheduler (mode 1):** ~15 `db.update_task_status(...)` sites; `_emit_event`
  (event log) and `_notify` (desktop notification) are sprinkled around some of
  them and absent from others. Whether a transition emits an event / sends a
  notification is a per-site decision, so a new code path that changes status
  can silently skip the effect. Desync surface.
- **Orchestrator (mode 2):** ~35 `db.update_workstream_status(...)` sites emit
  **no** events and **no** notifications at all. Workstream lifecycle is
  invisible to the event log and to notifications.

Additionally, some real transitions bypass the `update_*_status` API entirely
(atomic paths that must stay atomic — §4.3), and some `update_*_status` calls
are **not** transitions at all (same-state field patches — §4.2). A naive
"route everything through one method" would break both.

Maestro already has `correlation.CommonStatus` (a minimal common enum with
`PROJECTIONS` from both `TaskStatus`/`WorkstreamStatus` and a `_TRANSITIONS`
map). That is a **projection for the external WorkCorrelation contract** and is
deliberately out of scope here (§10).

## 2. Goal & non-goals

**Goal:** a single declarative source of truth mapping a status transition to
its side effects, and a small set of transition primitives in the two
orchestration layers (Scheduler, Orchestrator) that fire those effects so a
call site cannot change status and forget the effect.

**Non-goals (explicit):**

- Merging `TaskStatus` + `WorkstreamStatus` into one enum (this iteration is the
  hooks half, not the unification half of idea #10).
- Turning `correlation.CommonStatus` into an internal runtime state machine.
- `_emit_tick` (periodic snapshot, not a transition) and new transport buses
  (SSE etc.).
- **Transactional outbox / exactly-once delivery.** Effects fire *after* the DB
  commit as at-most-once best effort (§4.4). A crash between commit and fire
  loses that one effect. Guaranteed delivery would need an outbox; out of scope.
- **Intercepting every status mutation app-wide.** Statuses are also changed by
  the CLI (`maestro approve`, `maestro workstream-approve`), recovery, REST, and
  MCP. This iteration scopes *firing* to the Scheduler and Orchestrator loops.
  The effect table (§5) still documents the intended effect for those other
  transitions, so those layers can adopt the dispatcher later without
  re-deriving the mapping.

## 3. Architecture

New module **`maestro/transitions.py`** — the single source of truth, imported
by the orchestration layer (Scheduler, Orchestrator) but **never by
`database.py`** (the DB layer stays effect-free; §9 guards this).

### 3.1 The transition subject

Effects need a uniform, entity-agnostic view of "what transitioned":

```python
@dataclass(frozen=True)
class TransitionSubject:
    kind: Literal["task", "workstream"]
    id: str
    title: str
    status: TaskStatus | WorkstreamStatus  # the NEW status
```

Built from a `Task` or `Workstream` at the call site.

### 3.2 Generalized event/notification envelope

Both sinks are task-centric today and must carry a workstream without lying
about its type.

- **`event_log.Event`** gains `entity_type: Literal["task","workstream"] =
  "task"` and `entity_id: str | None`. The existing `task_id` field is **kept**
  (populated only for task events) so the persisted `events.jsonl` contract
  stays backward compatible — existing consumers keying on `task_id` keep
  working; new consumers read `entity_type`/`entity_id`. Workstream events set
  `entity_type="workstream"`, `entity_id=<ws id>`, `task_id=None`.
- **`notifications.Notification`** is generalized: `status: TaskStatus |
  WorkstreamStatus`, plus an `entity_kind: Literal["task","workstream"]` and
  entity-neutral `subject_id` / `subject_title`. `from_task` is kept and
  `from_workstream` added as the second factory. Channels format from the
  entity-neutral fields and the title reads "Workstream …" vs "Task …" from
  `entity_kind` — a channel never guesses the entity type. (Notification is a
  transient in-process object, not a persisted contract, so renaming the two
  fields is safe; the channel touch-points are updated in the same task.)

### 3.3 The dispatcher

```python
class TransitionDispatcher:
    def __init__(self, *, notifier, event_logger_getter, status_change_cb): ...

    async def fire(
        self,
        subject: TransitionSubject,
        *,
        frm: TaskStatus | WorkstreamStatus,
        details: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None: ...
```

`fire` is **async** (`NotificationManager.notify()` is async). It looks up the
effect for `(frm, subject.status)` (§5), then, for each configured subscriber
(all optional / None-safe):

- **event log** — logs `Event(event_type=effect.event, entity_type=subject.kind,
  entity_id=subject.id, task_id=<id if task else None>, message=message,
  details=details or {})` when `effect.event` is set.
- **notification** — `notifier.notify(Notification.from_<kind>(subject, effect.notification, message))` when `effect.notification` is set.
- **status-change callback** — the existing `_on_status_change(id, frm, to)`
  hook (dashboard feed), always called on a real transition.

`details`/`message` (error text, retry count, exit code, …) are supplied by the
call site — the table says *which* effect, the caller supplies *what data*.
No-op when `frm == subject.status` (not a transition) or the effect is empty.

## 4. Transition primitives (per orchestration class)

### 4.1 `_transition` — the normal path (write + dispatch)

```python
async def _transition(
    self, entity_id, to_status, *, expected_status, **fields
) -> Entity:
    entity = await self._db.update_*_status(
        entity_id, to_status, expected_status=expected_status, **fields
    )
    await self._dispatcher.fire(
        subject_of(entity), frm=expected_status, details=..., message=...
    )
    return entity
```

`expected_status` is **required**. `update_*_status(expected_status=…)` is a CAS;
on success `expected_status` **is** the true `frm` (a plain pre-`get` would be
unreliable under concurrent writes). If the CAS does not apply (row already
moved), no effect fires. `**fields` (e.g. `error_message`, `pr_url`) pass
through to the write. Optional `details`/`message` for the effect are threaded
in by the caller.

### 4.2 `_update_fields` — same-state patches (write, no dispatch)

Non-transition writes that reuse the status API to patch columns — e.g. the
orchestrator clearing `generation_pid` with a same-state write
(`orchestrator.py:550`) — go through a distinct helper that never dispatches:

```python
async def _update_fields(self, entity_id, **fields) -> Entity: ...
```

This keeps "state transition" and "column patch" from sharing one ambiguous
call. (Same-state writes carry no `expected_status`, matching today's behavior.)

### 4.3 `_dispatch_committed_transition` — atomic paths that bypass the write API

Some transitions are one atomic SQL unit and must stay so:

- `db.reset_for_retry_atomic()` — `FAILED → READY` with an arbiter guard,
  returns `ok: bool` (scheduler.py:575/1319/1420).
- (`db.approve_workstream_with_gate_record()` — raw-SQL `NEEDS_REVIEW → READY` +
  gate record — runs in the **CLI**, out of scope for firing this iteration; §2.)

These cannot become a plain `_transition` without losing atomicity. Instead, on
a **confirmed** success (`ok is True`) the caller fires the same dispatch:

```python
async def _dispatch_committed_transition(self, entity, *, frm) -> None:
    await self._dispatcher.fire(subject_of(entity), frm=frm)
```

For `reset_for_retry_atomic`, the `TASK_RETRYING` effect fires only on `ok=True`.

### 4.4 Delivery semantics

Effects fire after the DB commit — **at-most-once best effort**. A crash between
commit and `fire` drops that one effect; no rollback of the status. This is an
intentional limitation (outbox = non-goal, §2). Startup recovery already
reconciles *state*; it does not replay missed effects.

## 5. Effect tables (single source of truth)

Entry-based tables keyed on the target status, with a small pair-override table
for the from-dependent cases.

### 5.1 Totality (anti-desync at the table level)

```python
assert set(TASK_EFFECTS) == set(TaskStatus)
assert set(WORKSTREAM_EFFECTS) == set(WorkstreamStatus)
```

Every status has an **explicit** entry. "No effect" is written as
`StatusEffect()` (empty), **not** a missing key. A newly added enum member with
no table entry is a test failure, not a silent no-op — this is what makes the
table a real anti-desync gate.

```python
@dataclass(frozen=True)
class StatusEffect:
    event: EventType | None = None
    notification: NotificationEvent | None = None
```

### 5.2 What belongs in the table

Only genuine **status transitions**. Events that are operation *results* or
cross-cutting signals stay emitted at their call site and are **not** moved into
the table: arbiter routing events, `VALIDATION_STARTED/PASSED/FAILED` (a
validation outcome, not "entered DONE"), git events, and scheduler-lifecycle
events (`SCHEDULER_STARTED/STOPPED`). The table is for "entity entered status X",
nothing else.

### 5.3 Task table (encodes current scheduler behavior 1:1)

Derived verbatim from today's call sites so behavior is unchanged, e.g.:

| status (entry) | event | notification |
|---|---|---|
| `READY` | `TASK_READY` | — |
| `AWAITING_APPROVAL` | — | `TASK_AWAITING_APPROVAL` |
| `RUNNING` | `TASK_STARTED` | `TASK_STARTED` |
| `VALIDATING` | `StatusEffect()` (validation events fire at their own site) | — |
| `DONE` | `TASK_COMPLETED` | `TASK_COMPLETED` |
| `FAILED` | `TASK_FAILED` | `TASK_FAILED` |
| `NEEDS_REVIEW` | `TASK_NEEDS_REVIEW` | `TASK_NEEDS_REVIEW` |
| `ABANDONED` | `TASK_ABANDONED` | — |
| `PENDING` | `StatusEffect()` | — |

The exact per-status values are lifted from the current sites during
implementation; the table above is the shape, and the scheduler behavior-parity
tests (§9) pin that it matches today.

### 5.4 Pair overrides (from-aware)

`TRANSITION_OVERRIDES: dict[tuple[frm, to], StatusEffect]`, consulted before the
entry table:

| (frm → to) | effect | note |
|---|---|---|
| `FAILED → READY` | `TASK_RETRYING` / `WORKSTREAM_RETRYING` | retry, not a plain re-ready; fires from `reset_for_retry_atomic` on ok=True |
| `AWAITING_APPROVAL → READY` | `TASK_APPROVED` | approval, not plain ready (EventType already exists) |
| `NEEDS_REVIEW → READY` | approval/requeue event | operator requeue, not plain ready |

Overrides are **documented for all three** as the SSOT, but only fire where the
transition occurs inside Scheduler/Orchestrator (§2). `FAILED → READY` (retry)
fires from the scheduler. The two approval overrides currently transition in the
CLI and therefore do not fire this iteration — the table records their intended
effect for when the CLI/REST layers adopt the dispatcher. `WORKSTREAM_RETRYING`
is added to `EventType` (§6).

## 6. Mode-2 additions

- **`event_log.EventType`**: `WORKSTREAM_READY`, `WORKSTREAM_DECOMPOSING`,
  `WORKSTREAM_RUNNING`, `WORKSTREAM_MERGING`, `WORKSTREAM_PR_CREATED`,
  `WORKSTREAM_DONE`, `WORKSTREAM_FAILED`, `WORKSTREAM_NEEDS_REVIEW`,
  `WORKSTREAM_ABANDONED`, `WORKSTREAM_RETRYING`.
- **`notifications.NotificationEvent`** — **four** workstream notifications:
  `WORKSTREAM_STARTED` (on `RUNNING`), `WORKSTREAM_COMPLETED` (on `DONE`),
  `WORKSTREAM_FAILED` (on `FAILED`), `WORKSTREAM_NEEDS_REVIEW` (on
  `NEEDS_REVIEW`). **No** `WORKSTREAM_PR_CREATED` notification — `PR_CREATED` is
  an informational intermediate status the automatic flow continues past; it is
  not an operator gate. `NEEDS_REVIEW` **is** a gate and notifies. (Symmetric
  with tasks, which already notify on `TASK_NEEDS_REVIEW`.)
- **`Notification.from_workstream(ws, event, message=None)`** factory (§3.2).
- Orchestrator: the ~35 `update_workstream_status` sites are split into
  `_transition` (real transitions) and `_update_fields` (patches like
  `orchestrator.py:550`), and wired through the dispatcher.

## 7. Wiring the scheduler

The ~15 scheduler status sites move to `_transition` / the committed-transition
dispatch; the scattered `_emit_event`/`_notify` for **transition** events are
deleted (their mapping now lives in the table). Non-transition emits (arbiter,
validation, tick, lifecycle) are left exactly as they are.

## 8. Payload/message flow

`StatusEffect` says *which* event/notification. The variable data — error text,
`retry_count`, exit code, arbiter reason — is passed by the call site into
`fire(..., details=…, message=…)` and attached to the `Event`/`Notification`.
This preserves today's rich event payloads without putting call-site data into
the declarative table.

## 9. Testing

**`transitions.py` unit tests:**

- Totality: `set(TASK_EFFECTS) == set(TaskStatus)` and the workstream analog.
- `fire` invokes exactly the configured subscribers for a given effect; empty
  effect / same-state → no subscriber called; each subscriber None-safe.
- Pair overrides beat the entry table for `(FAILED, READY)` etc.
- Envelope: task event carries `task_id` + `entity_type="task"`; workstream
  event carries `entity_id` + `entity_type="workstream"` + `task_id=None`;
  `from_workstream` sets `entity_kind` and a `WorkstreamStatus`.

**Scheduler behavior-parity:** for each transition, the same event(s) and
notification(s) fire as before this change (pin the table against current
behavior); `reset_for_retry_atomic` fires `TASK_RETRYING` only on `ok=True`.

**Orchestrator:** each workstream transition fires its `WORKSTREAM_*` event; the
four notifications fire on `RUNNING`/`DONE`/`FAILED`/`NEEDS_REVIEW` and **not**
on `PR_CREATED`; `_update_fields` (generation_pid clear) fires nothing.

**Envelope back-compat:** an existing `events.jsonl` consumer keying on `task_id`
still reads task events unchanged.

**AST static guard** (over production files — a grep would be too brittle):

- In `scheduler.py`, a direct `self._db.update_task_status(...)` call is allowed
  **only** inside `_transition`.
- In `orchestrator.py`, a direct `self._db.update_workstream_status(...)` call is
  allowed **only** inside `_transition` or `_update_fields`.
- A call to a known atomic transition helper (`reset_for_retry_atomic`) in those
  files must be accompanied by a `_dispatch_committed_transition` on its success
  path.
- `maestro/transitions.py` is imported by the orchestration layer but **not** by
  `maestro/database.py`.

The guard asserts scope precisely — it does **not** claim "all `update_*_status`
everywhere go through `_transition`" (database tests, CLI, REST, MCP, recovery
legitimately call the DB API directly; §2).

## 10. Out of scope

- Enum unification (`TaskStatus` + `WorkstreamStatus`).
- `correlation.CommonStatus` as an internal state machine.
- Transactional outbox / exactly-once delivery.
- Firing effects for status mutations outside the Scheduler/Orchestrator loops
  (CLI approve, REST, MCP, recovery) — table documents intent; wiring deferred.
- `_emit_tick`, new transport buses, dashboard internals.
- `WORKSTREAM_PR_CREATED` notification.
