# Transition hooks — design

- **Date:** 2026-07-23
- **Status:** in review (brainstorming; architectural vector confirmed; r2 corrections folded in)
- **Scope:** idea #10 from `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`
  ("unified Phase enum + phase-transition hooks") — the **transition-hooks / anti-desync** half.

> **This is an intentional behavior change, not a refactor.** Effects bind to
> committed status transitions. That moves the moment of `TASK_STARTED`, adds
> task lifecycle events that were never emitted, and adds workstream
> events/notifications. See §0. Do not read any section as "1:1 parity".

## 0. Behavior changes (intentional)

Binding effects to committed transitions changes observable behavior. Each is
deliberate and covered by a **contract test** (§9), not a parity test:

- **`TASK_STARTED` notification timing.** Today it fires *after* a successful
  `backend.run()` (scheduler.py:1049) — it means "process launched". After this
  change it fires on entering `RUNNING` (before the launch attempt) — it means
  "status is running". A launch that fails immediately will now still have
  produced a `TASK_STARTED` notification.
- **New task lifecycle events.** Transitions like `READY`, `RUNNING`, `DONE`,
  `NEEDS_REVIEW`, `ABANDONED` did not emit `_emit_event` records before; they do
  now. Pure observability gain.
- **`FAILED` — event-only, symmetric for tasks and workstreams.** `FAILED` is
  transient and never terminal for either entity: a task's `FAILED` is always
  followed by `FAILED → READY` (retry) or `FAILED → NEEDS_REVIEW`, and a
  workstream's `FAILED` is always followed by the same pair. Both emit a
  `TASK_FAILED`/`WORKSTREAM_FAILED` event but **no notification** (matches
  today for tasks — the scheduler never notified on `FAILED`); a notification
  on `FAILED` would storm on every retry and double-notify once the status
  routes on to `NEEDS_REVIEW`. The actionable notification stays at
  `NEEDS_REVIEW`. *(This reverses an earlier draft of this section, which gave
  workstream `FAILED` its own notification on the theory that it was
  coarse-grained enough to be actionable on its own — final review concluded
  the transient/never-terminal argument dominates, so the two entities are
  symmetric.)*
- **`TASK_TIMEOUT` stays a call-site notification** (scheduler.py:1499) — it is
  an operation result (the monitor killed the process), not a bare transition;
  it is not moved into the table (§5.2).
- **Mode 2 gains events + three notifications** where it had none.

These are documented in the changelog on merge.

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
  commit as **best-effort, non-durable delivery** (§4.4). A crash between commit
  and fire loses that one effect; a re-run can re-deliver one. Guaranteed/deduped
  delivery would need an outbox; out of scope.
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

Built from a `Task` or `Workstream` at the call site. **It lives in
`maestro/models.py`** — the neutral module that already owns `TaskStatus` /
`WorkstreamStatus` and imports neither `notifications` nor `event_log`. This
placement is load-bearing: it breaks the import cycle that would otherwise form
(`transitions` → `notifications.base` → `transitions`), since
`Notification.from_subject` needs to reference `TransitionSubject` (§3.2).

### 3.2 Generalized event/notification envelope

Both sinks are task-centric today and must carry a workstream without lying
about its type.

- **`event_log.Event`** gains `entity_type: Literal["task","workstream"] =
  "task"` and `entity_id: str | None`. The existing `task_id` field is **kept**
  (populated only for task events) so the persisted `events.jsonl` contract
  stays backward compatible — existing consumers keying on `task_id` keep
  working; new consumers read `entity_type`/`entity_id`. Workstream events set
  `entity_type="workstream"`, `entity_id=<ws id>`, `task_id=None`. The event's
  JSONL serializer (`to_json_line()`) now emits `entity_type`/`entity_id`
  alongside `task_id`. **Backward compatibility here means the `task_id` field
  is preserved for task events, not that the serialized JSON is byte-identical**
  (task events gain the two new keys).
- **`notifications.Notification`** is generalized: `status: TaskStatus |
  WorkstreamStatus`, plus `entity_kind: Literal["task","workstream"]` and
  entity-neutral `subject_id` / `subject_title` (replacing `task_id` /
  `task_title`). Channels format from the entity-neutral fields and the title
  reads "Workstream …" vs "Task …" from `entity_kind` — a channel never guesses
  the entity type. (Notification is a transient in-process object, not a
  persisted contract, so renaming the two fields is safe; the channel
  touch-points are updated in the same task.)

**One construction contract — `from_subject`.** The dispatcher is
entity-agnostic and must not pick a factory by kind at call time. The single
constructor it calls is:

```python
@classmethod
def from_subject(
    cls,
    subject: TransitionSubject,
    event: NotificationEvent,
    message: str | None = None,
) -> Notification: ...
```

`from_task(task, event, message)` and `from_workstream(ws, event, message)` are
kept as convenience adapters that build a `TransitionSubject` and delegate to
`from_subject`. Same pattern for the event: the dispatcher builds the `Event`
from the `subject`, never from a raw `Task`/`Workstream`.

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

`fire` is **async** (`NotificationManager.notify()` is async). It selects the
effect table by `subject.kind` (the `TASK_*` tables for a task, the
`WORKSTREAM_*` tables for a workstream — never a cross-entity lookup; §5.4), then
looks up the effect for `(frm, subject.status)` and drives the subscribers.

### 3.3.1 Subscriber contract (resolves the empty-effect vs callback case)

The status-change callback is **orthogonal to the effect table** — it reports
*that* a transition happened, independent of whether the transition emits an
event/notification. Precise contract:

| condition | status callback | event sink | notification sink |
|---|---|---|---|
| `frm == to` (not a transition) | — | — | — |
| real transition, **empty** effect | ✅ called | — | — |
| real transition, effect has event/notif | ✅ called | ✅ if `effect.event` | ✅ if `effect.notification` |

Total tables (§5.1) guarantee there is no fourth "unknown" combination. So
`VALIDATING`/`PENDING` (empty effects) still fire the dashboard callback on a
real transition — they just emit no event/notification.

- **status-change callback** — the existing `StatusChangeCallback =
  Callable[[str, str, str], None]` (dashboard feed), called on every real
  transition **with plain strings**: `status_change_cb(subject.id, frm.value,
  subject.status.value)`. The callback contract is string-based (the CLI feed
  compares `new_status == "running"` / `"done"`, cli.py:539); passing raw
  `StrEnum` members would "work" by accident, so the dispatcher passes `.value`
  explicitly and the tests assert strings.
- **event log** — `Event(event_type=effect.event, entity_type=subject.kind,
  entity_id=subject.id, task_id=<id if task else None>, message=message,
  details=details or {})` when `effect.event` is set.
- **notification** — `notifier.notify(Notification.from_subject(subject,
  effect.notification, message))` when `effect.notification` is set.

`details`/`message` (error text, retry count, exit code, …) are supplied by the
call site — the table says *which* effect, the caller supplies *what data*.

### 3.3.2 Fail-isolation (subscribers run after commit)

`fire` runs **after** the DB commit, so a subscriber raising must not corrupt the
already-committed transition:

- each subscriber is invoked in its **own** `try/except`;
- an exception is logged and **swallowed** — the remaining subscribers still run;
- `fire` never propagates a subscriber exception to its caller after a committed
  transition (a raise there would make the caller think the transition failed,
  and a retry would hit the CAS);
- `asyncio.CancelledError` is the one exception **re-raised**, never swallowed —
  cooperative cancellation must not be masked.

This matters most for the status callback, which today is called directly and
could throw (scheduler.py:295).

## 4. Transition primitives (per orchestration class)

### 4.1 `_transition` — the normal path (write + dispatch)

```python
async def _transition(
    self,
    entity_id,
    to_status,
    *,
    expected_status,
    details: dict | None = None,   # keyword-only: for the effect, NOT the DB
    message: str | None = None,    # keyword-only: for the effect, NOT the DB
    **fields,                      # DB columns only (error_message, pr_url, …)
) -> Entity:
    entity = await self._db.update_*_status(
        entity_id, to_status, expected_status=expected_status, **fields
    )
    await self._dispatcher.fire(
        subject_of(entity), frm=expected_status, details=details, message=message
    )
    return entity
```

`expected_status` is **required**. `update_*_status(expected_status=…)` is a CAS;
on success `expected_status` **is** the true `frm` (a plain pre-`get` would be
unreliable under concurrent writes). If the CAS does not apply (row already
moved), no effect fires. `details`/`message` are **explicit keyword-only**
parameters for the effect — kept out of `**fields` so they can never leak into
the DB column patch. `**fields` (e.g. `error_message`, `pr_url`) pass through to
the write only.

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

`reset_for_retry_atomic()` returns only a `bool`, so the caller must
**re-read** the entity after `ok is True` (`entity = await
self._db.get_task(task_id)`) to build an accurate `TransitionSubject` before
dispatching. The `TASK_RETRYING` effect fires only on `ok=True`; on `ok=False`
(guard rejected the reset) nothing fires.

### 4.4 Delivery semantics

Effects fire after the DB commit — **best-effort, non-durable delivery**. A
crash between commit and `fire` drops that one effect (no status rollback), and
a re-run of the orchestration code over the same state can in principle
re-deliver an effect — the system provides **no** strict at-most-once or
exactly-once guarantee without a durable delivery marker. Guaranteed/deduped
delivery would need an outbox (non-goal, §2). Startup recovery reconciles
*state*; it does not replay or dedupe effects.

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

### 5.3 Task table (intentional behavior — see §0, NOT parity)

This table binds effects to committed transitions. It is a deliberate behavior
**expansion** over today's scattered emits (§0): selected lifecycle status
entries now emit events according to the total table (`PENDING`,
`AWAITING_APPROVAL`, `VALIDATING` remain empty-event by design); notifications
are chosen for hygiene (no notification on the transient `FAILED`; the actionable
notification stays at `NEEDS_REVIEW`).

| status (entry) | event | notification | vs today |
|---|---|---|---|
| `PENDING` | `StatusEffect()` | — | unchanged (no effect) |
| `READY` | `TASK_READY` | — | **new event** |
| `AWAITING_APPROVAL` | `StatusEffect()` | `TASK_AWAITING_APPROVAL` | notif unchanged¹ |
| `RUNNING` | `TASK_STARTED` | `TASK_STARTED` | **new event**; notif **timing shifts** to entry (§0) |
| `VALIDATING` | `StatusEffect()` | — | unchanged (validation events fire at their own site) |
| `DONE` | `TASK_COMPLETED` | `TASK_COMPLETED` | **new event**; notif unchanged |
| `FAILED` | `TASK_FAILED` | — | **new event; deliberately NO notif** (§0) |
| `NEEDS_REVIEW` | `TASK_NEEDS_REVIEW` | `TASK_NEEDS_REVIEW` | **new event**; notif unchanged |
| `ABANDONED` | `TASK_ABANDONED` | — | **new event** |

¹ Where the `AWAITING_APPROVAL` transition occurs inside the scheduler loop;
if it is CLI-driven it is out of scope for firing (§2), but the entry is the SSOT.

`TASK_TIMEOUT` is **not** in the table (call-site notification, §0/§5.2). The
exact "vs today" mapping is pinned by the contract tests (§9).

### 5.4 Pair overrides (from-aware)

**Two separate tables**, symmetric with `TASK_EFFECTS`/`WORKSTREAM_EFFECTS`:

```python
TASK_TRANSITION_OVERRIDES: dict[tuple[TaskStatus, TaskStatus], StatusEffect]
WORKSTREAM_TRANSITION_OVERRIDES: dict[
    tuple[WorkstreamStatus, WorkstreamStatus], StatusEffect
]
```

They must **not** share one dict. `TaskStatus` and `WorkstreamStatus` are both
`StrEnum` with overlapping values (`"failed"`, `"ready"`, `"needs_review"`), so
`(TaskStatus.FAILED, TaskStatus.READY)` and `(WorkstreamStatus.FAILED,
WorkstreamStatus.READY)` are **equal keys** — a shared dict silently collapses
them (verified: the workstream entry overwrites the task entry, len 1). The
dispatcher picks the table by `subject.kind` (§3.3), so there is no cross-entity
lookup. Every entry names concrete enums so the SSOT is complete:

| (frm → to) | task effect | workstream effect | note |
|---|---|---|---|
| `FAILED → READY` | `StatusEffect(TASK_RETRYING)` | `StatusEffect(WORKSTREAM_RETRYING)` | retry, not plain re-ready; fires from `reset_for_retry_atomic` on ok=True |
| `AWAITING_APPROVAL → READY` | `StatusEffect(TASK_APPROVED)` | — (workstreams have no `AWAITING_APPROVAL`) | approval before first run |
| `NEEDS_REVIEW → READY` | `StatusEffect(TASK_APPROVED)` | `StatusEffect(WORKSTREAM_APPROVED)` | operator requeue after review |

`TASK_APPROVED` already exists in `EventType`; `WORKSTREAM_RETRYING` and
`WORKSTREAM_APPROVED` are added (§6).

**Firing scope.** Overrides fire only where the transition occurs inside
Scheduler/Orchestrator (§2). `FAILED → READY` (retry) fires from the scheduler.
The two approval overrides currently transition in the **CLI** (`maestro
approve`, `maestro workstream-approve`), so they do **not** fire this iteration —
the table records their intended effect for when the CLI/REST layer adopts the
dispatcher. The added `WORKSTREAM_APPROVED` event therefore has no emitter yet;
it exists to keep the override SSOT concrete and complete (documented as
deferred-emit).

## 6. Mode-2 additions

- **`event_log.EventType`**: `WORKSTREAM_READY`, `WORKSTREAM_DECOMPOSING`,
  `WORKSTREAM_RUNNING`, `WORKSTREAM_MERGING`, `WORKSTREAM_PR_CREATED`,
  `WORKSTREAM_DONE`, `WORKSTREAM_FAILED`, `WORKSTREAM_NEEDS_REVIEW`,
  `WORKSTREAM_ABANDONED`, `WORKSTREAM_RETRYING`, `WORKSTREAM_APPROVED`
  (`WORKSTREAM_APPROVED` is deferred-emit — §5.4).
- **`notifications.NotificationEvent`** — **three** workstream notifications:
  `WORKSTREAM_STARTED` (on `RUNNING`), `WORKSTREAM_COMPLETED` (on `DONE`),
  `WORKSTREAM_NEEDS_REVIEW` (on `NEEDS_REVIEW`). **No** `WORKSTREAM_FAILED`
  notification — `FAILED` is transient and never terminal (always followed by
  `FAILED → READY` retry or `FAILED → NEEDS_REVIEW`), so it is event-only,
  symmetric with the task side (§0). **No** `WORKSTREAM_PR_CREATED`
  notification either — `PR_CREATED` is an informational intermediate status the
  automatic flow continues past; it is not an operator gate. `NEEDS_REVIEW`
  **is** a gate and notifies.
  *(This reverses an earlier draft's four-notification decision, which gave
  `FAILED` its own notification — see §0.)*
- **`Notification.from_workstream(ws, event, message=None)`** convenience adapter
  → `from_subject` (§3.2).

The workstream effect table (§5.1 total): `RUNNING →
(WORKSTREAM_RUNNING event, WORKSTREAM_STARTED notif)`, `DONE → (WORKSTREAM_DONE,
WORKSTREAM_COMPLETED)`, `FAILED → (WORKSTREAM_FAILED, —)`,
`NEEDS_REVIEW → (WORKSTREAM_NEEDS_REVIEW, WORKSTREAM_NEEDS_REVIEW)`, `MERGING →
(WORKSTREAM_MERGING, —)`, `PR_CREATED → (WORKSTREAM_PR_CREATED, —)`,
`DECOMPOSING → (WORKSTREAM_DECOMPOSING, —)`, `READY → (WORKSTREAM_READY, —)`,
`ABANDONED → (WORKSTREAM_ABANDONED, —)`, `PENDING → StatusEffect()`.
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
- Subscriber contract (§3.3.1) — the full truth table:
  - `frm == to` → **no** subscriber called (not even the callback).
  - real transition + **empty** effect → **only** the status callback fires
    (event/notification sinks silent). Assert this for `VALIDATING`/`PENDING`.
  - real transition + event/notif effect → callback **plus** the matching sinks.
  - each sink None-safe (unconfigured subscriber skipped).
- Pair overrides beat the entry table for `(FAILED, READY)` etc.
- **Fail-isolation (§3.3.2):** a subscriber that raises is logged and swallowed,
  the other subscribers still fire, and `fire` does not propagate the exception;
  a subscriber raising `asyncio.CancelledError` **is** re-raised (not swallowed).
- Envelope: task event carries `task_id` + `entity_type="task"`; workstream
  event carries `entity_id` + `entity_type="workstream"` + `task_id=None`;
  `from_workstream`/`from_task` delegate to `from_subject` and set `entity_kind`
  + the correct `Task|WorkstreamStatus`.

**Scheduler contract tests (behavior change — §0, NOT parity):** assert the
*new* contract per transition — the intended event(s) and notification(s) fire,
including the documented changes: `TASK_STARTED` notification now fires on
entering `RUNNING`; `FAILED` emits `TASK_FAILED` event with **no** notification;
new lifecycle events appear; `TASK_TIMEOUT` still fires at its call site.
`reset_for_retry_atomic` fires `TASK_RETRYING` only on `ok=True`, and the
subject is built from the **re-read** task (§4.3).

**Orchestrator:** each workstream transition fires its `WORKSTREAM_*` event; the
three notifications fire on `RUNNING`/`DONE`/`NEEDS_REVIEW` and **not** on
`FAILED` (event-only, transient — §0) or `PR_CREATED`/`MERGING`/`DECOMPOSING`;
`_update_fields` (generation_pid clear, `orchestrator.py:550`) fires nothing.

**Envelope back-compat:** an existing `events.jsonl` consumer keying on `task_id`
still reads task events unchanged.

**AST static guard** (over production files — a grep would be too brittle):

- In `scheduler.py`, a direct `self._db.update_task_status(...)` call is allowed
  **only** inside `_transition` (AST: check the name of the enclosing
  `FunctionDef`).
- In `orchestrator.py`, a direct `self._db.update_workstream_status(...)` call is
  allowed **only** inside `_transition` or `_update_fields` (same enclosing-def
  check).
- `maestro/transitions.py` is imported by the orchestration layer but **not** by
  `maestro/database.py`.

The atomic-helper rule is **not** a self-built control-flow analysis (that is
too heavy and brittle). Instead: an AST test asserts each
`reset_for_retry_atomic` call in `scheduler.py` sits inside a function that also
references `_dispatch_committed_transition`, plus a behavioral unit test that the
`TASK_RETRYING` effect fires on `ok=True` and does **not** on `ok=False`. The
behavior test is the real guarantee; the AST check is a cheap tripwire.

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
