# Transition hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route status transitions in the Scheduler and Orchestrator through a single declarative "transition → effects" dispatcher, so a call site cannot change status and forget the side effect (event log / notification / dashboard callback).

**Architecture:** A pure `transitions.py` holds `StatusEffect`, per-entity effect + override tables, and an async `TransitionDispatcher`. `TransitionSubject` lives in `models.py` (neutral, breaks the import cycle). `Event`/`Notification` are generalized to carry a task OR a workstream. Scheduler and Orchestrator get `_transition` / `_update_fields` / `_dispatch_committed_transition` primitives. This is an intentional behavior change (§0 of the spec), not a refactor.

**Tech Stack:** Python 3.12+, pydantic, StrEnum, dataclasses, pytest (pytest-asyncio auto), ruff, pyrefly, `ast` (guard test).

## Global Constraints

- Package manager: `uv` only (`uv run pytest`, `uv add`). Never pip.
- Type hints on all code; `uv run pyrefly check` must report 0 errors.
- `uv run ruff format .` and `uv run ruff check .` must pass; line length 88.
- Async tests: `async def test_...` under pytest-asyncio auto mode (repo convention).
- Public functions get docstrings. Follow existing patterns in `maestro/`.
- This is an **intentional behavior change**, not parity: `TASK_STARTED`
  notification moves to the `RUNNING` transition (fires before launch); new task
  lifecycle events appear; `FAILED` emits an event but (for tasks) no
  notification; workstreams gain events + four notifications. Tests are
  **contract** tests of the new behavior.
- Approval-marker / arbiter / validation / git / scheduler-lifecycle events are
  NOT transitions — leave them emitted at their call sites.
- Spec: `docs/superpowers/specs/2026-07-23-maestro-transition-hooks-design.md`.

## File Structure

- Create `maestro/transitions.py` — `StatusEffect`, effect + override tables, `TransitionDispatcher`.
- Modify `maestro/models.py` — add `TransitionSubject` (neutral home).
- Modify `maestro/event_log.py` — `Event` gains `entity_type`/`entity_id`; add `WORKSTREAM_*` `EventType` values.
- Modify `maestro/notifications/base.py` — generalize `Notification` (`from_subject`, `entity_kind`, `subject_id`/`subject_title`, `status` union); add `WORKSTREAM_*` `NotificationEvent`.
- Modify `maestro/scheduler.py` — `_transition`, `_dispatch_committed_transition`; route sites through the dispatcher.
- Modify `maestro/orchestrator.py` — add subscribers to `__init__`; `_transition`, `_update_fields`; route sites.
- Tests: `tests/test_transitions.py`, `tests/test_transition_guard.py`, plus additions to `tests/test_scheduler.py` / `tests/test_orchestrator.py` and `tests/test_event_log.py` / `tests/test_notifications.py`.

---

## Task 1: Foundational types — `TransitionSubject` + `StatusEffect`

**Files:**
- Modify: `maestro/models.py` (add `TransitionSubject`)
- Create: `maestro/transitions.py` (add `StatusEffect` only)
- Test: `tests/test_transitions.py`

**Interfaces:**
- Consumes: `TaskStatus`, `WorkstreamStatus` (already in models.py).
- Produces:
  - `models.TransitionSubject` — frozen dataclass `kind: Literal["task","workstream"]`, `id: str`, `title: str`, `status: TaskStatus | WorkstreamStatus`.
  - `transitions.StatusEffect` — frozen dataclass `event: EventType | None = None`, `notification: NotificationEvent | None = None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transitions.py
from maestro.models import TaskStatus, TransitionSubject, WorkstreamStatus
from maestro.transitions import StatusEffect


def test_transition_subject_holds_either_status():
    t = TransitionSubject(kind="task", id="t1", title="T", status=TaskStatus.RUNNING)
    w = TransitionSubject(
        kind="workstream", id="w1", title="W", status=WorkstreamStatus.MERGING
    )
    assert t.kind == "task" and t.status == TaskStatus.RUNNING
    assert w.kind == "workstream" and w.status == WorkstreamStatus.MERGING


def test_status_effect_defaults_empty():
    e = StatusEffect()
    assert e.event is None and e.notification is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transitions.py -q`
Expected: FAIL — `ImportError` (`TransitionSubject` / `StatusEffect` missing).

- [ ] **Step 3: Implement**

In `maestro/models.py` (near the status enums; use the existing import style — `from dataclasses import dataclass` if not already imported, else add):

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TransitionSubject:
    """Entity-agnostic view of a committed status transition's subject.

    Lives here (not in transitions.py) so notifications/base.py can reference it
    without a transitions <-> notifications import cycle.
    """

    kind: Literal["task", "workstream"]
    id: str
    title: str
    status: "TaskStatus | WorkstreamStatus"
```

Create `maestro/transitions.py`:

```python
"""Declarative status-transition -> side-effect mapping and dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maestro.event_log import EventType
    from maestro.notifications.base import NotificationEvent


@dataclass(frozen=True)
class StatusEffect:
    """The side effect of entering (or transitioning into) a status."""

    event: "EventType | None" = None
    notification: "NotificationEvent | None" = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transitions.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format maestro/models.py maestro/transitions.py tests/test_transitions.py && uv run ruff check maestro/models.py maestro/transitions.py tests/test_transitions.py && uv run pyrefly check`
Expected: clean, 0 errors.

- [ ] **Step 6: Commit**

```bash
git add maestro/models.py maestro/transitions.py tests/test_transitions.py
git commit -m "feat(transitions): TransitionSubject (models) + StatusEffect"
```

---

## Task 2: Event envelope — `entity_type`/`entity_id` + `WORKSTREAM_*` events

**Files:**
- Modify: `maestro/event_log.py`
- Test: `tests/test_event_log.py`

**Interfaces:**
- Produces: `Event.entity_type: Literal["task","workstream"] = "task"`, `Event.entity_id: str | None = None`; `to_json_line()` emits both; new `EventType` members `WORKSTREAM_READY`, `WORKSTREAM_DECOMPOSING`, `WORKSTREAM_RUNNING`, `WORKSTREAM_MERGING`, `WORKSTREAM_PR_CREATED`, `WORKSTREAM_DONE`, `WORKSTREAM_FAILED`, `WORKSTREAM_NEEDS_REVIEW`, `WORKSTREAM_ABANDONED`, `WORKSTREAM_RETRYING`, `WORKSTREAM_APPROVED`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_log.py  (add)
import json
from maestro.event_log import Event, EventType


def test_event_has_entity_envelope_defaulting_to_task():
    e = Event(event_type=EventType.TASK_STARTED, task_id="t1")
    assert e.entity_type == "task"
    assert e.entity_id is None  # task events keep using task_id
    line = json.loads(e.to_json_line())
    assert line["task_id"] == "t1"
    assert line["entity_type"] == "task"


def test_workstream_event_uses_entity_id():
    e = Event(
        event_type=EventType.WORKSTREAM_RUNNING,
        entity_type="workstream",
        entity_id="w1",
    )
    line = json.loads(e.to_json_line())
    assert line["entity_type"] == "workstream"
    assert line["entity_id"] == "w1"
    assert line["task_id"] is None


def test_all_workstream_event_types_exist():
    for name in [
        "WORKSTREAM_READY", "WORKSTREAM_DECOMPOSING", "WORKSTREAM_RUNNING",
        "WORKSTREAM_MERGING", "WORKSTREAM_PR_CREATED", "WORKSTREAM_DONE",
        "WORKSTREAM_FAILED", "WORKSTREAM_NEEDS_REVIEW", "WORKSTREAM_ABANDONED",
        "WORKSTREAM_RETRYING", "WORKSTREAM_APPROVED",
    ]:
        assert hasattr(EventType, name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_event_log.py -q -k "entity or workstream"`
Expected: FAIL — attributes / EventType members missing.

- [ ] **Step 3: Implement**

Add the `WORKSTREAM_*` members to `EventType` (after the `TASK_*` block):

```python
    # Workstream lifecycle (mode 2)
    WORKSTREAM_READY = "workstream_ready"
    WORKSTREAM_DECOMPOSING = "workstream_decomposing"
    WORKSTREAM_RUNNING = "workstream_running"
    WORKSTREAM_MERGING = "workstream_merging"
    WORKSTREAM_PR_CREATED = "workstream_pr_created"
    WORKSTREAM_DONE = "workstream_done"
    WORKSTREAM_FAILED = "workstream_failed"
    WORKSTREAM_NEEDS_REVIEW = "workstream_needs_review"
    WORKSTREAM_ABANDONED = "workstream_abandoned"
    WORKSTREAM_RETRYING = "workstream_retrying"
    WORKSTREAM_APPROVED = "workstream_approved"  # deferred-emit (spec §5.4)
```

Add fields to `Event` (after `task_id`):

```python
    entity_type: Literal["task", "workstream"] = "task"
    entity_id: str | None = None
```

Import `Literal` if not present. In `to_json_line()`, include `entity_type` and
`entity_id` in the serialized dict (keep `task_id` — do not remove or rename it).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_event_log.py -q`
Expected: PASS.

- [ ] **Step 5: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green (existing event_log consumers unaffected).

- [ ] **Step 6: Commit**

```bash
git add maestro/event_log.py tests/test_event_log.py
git commit -m "feat(event-log): entity_type/entity_id envelope + WORKSTREAM_* events"
```

---

## Task 3: Notification generalization + `WORKSTREAM_*` notifications

**Files:**
- Modify: `maestro/notifications/base.py`
- Test: `tests/test_notifications.py`

**Interfaces:**
- Consumes: `TransitionSubject` (Task 1).
- Produces:
  - `NotificationEvent` gains `WORKSTREAM_STARTED`, `WORKSTREAM_COMPLETED`, `WORKSTREAM_FAILED`, `WORKSTREAM_NEEDS_REVIEW`.
  - `Notification` fields: `event`, `subject_id: str`, `subject_title: str`, `entity_kind: Literal["task","workstream"]`, `status: TaskStatus | WorkstreamStatus`, `message`.
  - `Notification.from_subject(subject: TransitionSubject, event, message=None)` — the dispatcher's single constructor.
  - `from_task(task, event, message=None)` and `from_workstream(ws, event, message=None)` — adapters that build a `TransitionSubject` and delegate to `from_subject`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notifications.py  (add)
from maestro.models import TaskStatus, TransitionSubject, WorkstreamStatus
from maestro.notifications.base import Notification, NotificationEvent


def test_from_subject_task():
    s = TransitionSubject(kind="task", id="t1", title="T", status=TaskStatus.RUNNING)
    n = Notification.from_subject(s, NotificationEvent.TASK_STARTED)
    assert n.subject_id == "t1" and n.subject_title == "T"
    assert n.entity_kind == "task" and n.status == TaskStatus.RUNNING
    assert "Task" in n.format_title()
    assert "[t1] T" in n.format_body()


def test_from_subject_workstream_wording():
    s = TransitionSubject(
        kind="workstream", id="w1", title="W", status=WorkstreamStatus.DONE
    )
    n = Notification.from_subject(s, NotificationEvent.WORKSTREAM_COMPLETED)
    assert n.entity_kind == "workstream"
    assert "Workstream" in n.format_title()  # channel never guesses the kind


def test_from_workstream_adapter_delegates():
    from maestro.models import Workstream
    ws = Workstream(id="w1", title="W", description="d", branch="feature/w1")
    n = Notification.from_workstream(ws, NotificationEvent.WORKSTREAM_STARTED)
    assert n.entity_kind == "workstream" and n.subject_id == "w1"


def test_four_workstream_notification_events_exist():
    for name in [
        "WORKSTREAM_STARTED", "WORKSTREAM_COMPLETED",
        "WORKSTREAM_FAILED", "WORKSTREAM_NEEDS_REVIEW",
    ]:
        assert hasattr(NotificationEvent, name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notifications.py -q -k "subject or workstream"`
Expected: FAIL — missing members/fields/constructor.

- [ ] **Step 3: Implement**

Add to `NotificationEvent`:

```python
    WORKSTREAM_STARTED = "workstream_started"
    WORKSTREAM_COMPLETED = "workstream_completed"
    WORKSTREAM_FAILED = "workstream_failed"
    WORKSTREAM_NEEDS_REVIEW = "workstream_needs_review"
```

Generalize `Notification`: rename `task_id`/`task_title` → `subject_id`/`subject_title`, add `entity_kind: Literal["task","workstream"]`, widen `status: TaskStatus | WorkstreamStatus`. Replace the constructors:

```python
    @classmethod
    def from_subject(
        cls,
        subject: "TransitionSubject",
        event: NotificationEvent,
        message: str | None = None,
    ) -> "Notification":
        return cls(
            event=event,
            subject_id=subject.id,
            subject_title=subject.title,
            entity_kind=subject.kind,
            status=subject.status,
            message=message,
        )

    @classmethod
    def from_task(cls, task, event, message=None):
        from maestro.models import TransitionSubject
        return cls.from_subject(
            TransitionSubject("task", task.id, task.title, task.status), event, message
        )

    @classmethod
    def from_workstream(cls, ws, event, message=None):
        from maestro.models import TransitionSubject
        return cls.from_subject(
            TransitionSubject("workstream", ws.id, ws.title, ws.status), event, message
        )
```

Update `format_title()`: add the four `WORKSTREAM_*` entries ("Workstream Started"
etc.) and derive the "Task"/"Workstream" word from `entity_kind` for the fallback.
Update `format_body()` to use `subject_id`/`subject_title`.

- [ ] **Step 4: Run test + full suite (from_task callers still work)**

Run: `uv run pytest tests/test_notifications.py -q && uv run pytest -q`
Expected: PASS. Existing `Notification.from_task(...)` scheduler calls still
compile and behave (same fields under new names via the adapter).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean. Fix any caller of `.task_id`/`.task_title` on a `Notification`
(grep `\.task_id`/`\.task_title` in `maestro/notifications/` and tests).

- [ ] **Step 6: Commit**

```bash
git add maestro/notifications/base.py tests/test_notifications.py
git commit -m "feat(notifications): generalize Notification (from_subject) + WORKSTREAM_* events"
```

---

## Task 4: Effect + override tables (total, per-entity)

**Files:**
- Modify: `maestro/transitions.py`
- Test: `tests/test_transitions.py`

**Interfaces:**
- Consumes: `EventType` (Task 2), `NotificationEvent` (Task 3), `TaskStatus`/`WorkstreamStatus`, `StatusEffect`.
- Produces: `TASK_EFFECTS`, `WORKSTREAM_EFFECTS` (entry tables), `TASK_TRANSITION_OVERRIDES`, `WORKSTREAM_TRANSITION_OVERRIDES` (pair tables). All total over their enum.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transitions.py  (add)
from maestro.models import TaskStatus, WorkstreamStatus
from maestro.event_log import EventType
from maestro.notifications.base import NotificationEvent
from maestro.transitions import (
    TASK_EFFECTS, WORKSTREAM_EFFECTS,
    TASK_TRANSITION_OVERRIDES, WORKSTREAM_TRANSITION_OVERRIDES,
)


def test_effect_tables_are_total():
    assert set(TASK_EFFECTS) == set(TaskStatus)
    assert set(WORKSTREAM_EFFECTS) == set(WorkstreamStatus)


def test_task_running_effect():
    e = TASK_EFFECTS[TaskStatus.RUNNING]
    assert e.event == EventType.TASK_STARTED
    assert e.notification == NotificationEvent.TASK_STARTED


def test_task_failed_event_but_no_notification():
    e = TASK_EFFECTS[TaskStatus.FAILED]
    assert e.event == EventType.TASK_FAILED
    assert e.notification is None  # transient failures don't notify (spec §0)


def test_workstream_failed_notifies():
    e = WORKSTREAM_EFFECTS[WorkstreamStatus.FAILED]
    assert e.event == EventType.WORKSTREAM_FAILED
    assert e.notification == NotificationEvent.WORKSTREAM_FAILED


def test_pr_created_has_no_notification():
    assert WORKSTREAM_EFFECTS[WorkstreamStatus.PR_CREATED].notification is None


def test_override_tables_do_not_collide_across_entities():
    # StrEnum values overlap; separate dicts must each keep their own entry.
    assert TASK_TRANSITION_OVERRIDES[(TaskStatus.FAILED, TaskStatus.READY)].event \
        == EventType.TASK_RETRYING
    assert WORKSTREAM_TRANSITION_OVERRIDES[
        (WorkstreamStatus.FAILED, WorkstreamStatus.READY)
    ].event == EventType.WORKSTREAM_RETRYING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transitions.py -q -k "effect or override"`
Expected: FAIL — tables not defined.

- [ ] **Step 3: Implement the tables** (values verbatim from spec §5.3 / §6 / §5.4)

```python
# maestro/transitions.py  (add real imports at top, not TYPE_CHECKING, for the tables)
from maestro.event_log import EventType
from maestro.models import TaskStatus, WorkstreamStatus
from maestro.notifications.base import NotificationEvent

TASK_EFFECTS: dict[TaskStatus, StatusEffect] = {
    TaskStatus.PENDING: StatusEffect(),
    TaskStatus.READY: StatusEffect(event=EventType.TASK_READY),
    TaskStatus.AWAITING_APPROVAL: StatusEffect(
        notification=NotificationEvent.TASK_AWAITING_APPROVAL
    ),
    TaskStatus.RUNNING: StatusEffect(
        event=EventType.TASK_STARTED, notification=NotificationEvent.TASK_STARTED
    ),
    TaskStatus.VALIDATING: StatusEffect(),
    TaskStatus.DONE: StatusEffect(
        event=EventType.TASK_COMPLETED, notification=NotificationEvent.TASK_COMPLETED
    ),
    TaskStatus.FAILED: StatusEffect(event=EventType.TASK_FAILED),
    TaskStatus.NEEDS_REVIEW: StatusEffect(
        event=EventType.TASK_NEEDS_REVIEW,
        notification=NotificationEvent.TASK_NEEDS_REVIEW,
    ),
    TaskStatus.ABANDONED: StatusEffect(event=EventType.TASK_ABANDONED),
}

WORKSTREAM_EFFECTS: dict[WorkstreamStatus, StatusEffect] = {
    WorkstreamStatus.PENDING: StatusEffect(),
    WorkstreamStatus.READY: StatusEffect(event=EventType.WORKSTREAM_READY),
    WorkstreamStatus.DECOMPOSING: StatusEffect(event=EventType.WORKSTREAM_DECOMPOSING),
    WorkstreamStatus.RUNNING: StatusEffect(
        event=EventType.WORKSTREAM_RUNNING,
        notification=NotificationEvent.WORKSTREAM_STARTED,
    ),
    WorkstreamStatus.MERGING: StatusEffect(event=EventType.WORKSTREAM_MERGING),
    WorkstreamStatus.PR_CREATED: StatusEffect(event=EventType.WORKSTREAM_PR_CREATED),
    WorkstreamStatus.DONE: StatusEffect(
        event=EventType.WORKSTREAM_DONE,
        notification=NotificationEvent.WORKSTREAM_COMPLETED,
    ),
    WorkstreamStatus.FAILED: StatusEffect(
        event=EventType.WORKSTREAM_FAILED,
        notification=NotificationEvent.WORKSTREAM_FAILED,
    ),
    WorkstreamStatus.NEEDS_REVIEW: StatusEffect(
        event=EventType.WORKSTREAM_NEEDS_REVIEW,
        notification=NotificationEvent.WORKSTREAM_NEEDS_REVIEW,
    ),
    WorkstreamStatus.ABANDONED: StatusEffect(event=EventType.WORKSTREAM_ABANDONED),
}

TASK_TRANSITION_OVERRIDES: dict[tuple[TaskStatus, TaskStatus], StatusEffect] = {
    (TaskStatus.FAILED, TaskStatus.READY): StatusEffect(event=EventType.TASK_RETRYING),
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.READY): StatusEffect(
        event=EventType.TASK_APPROVED
    ),
    (TaskStatus.NEEDS_REVIEW, TaskStatus.READY): StatusEffect(
        event=EventType.TASK_APPROVED
    ),
}

WORKSTREAM_TRANSITION_OVERRIDES: dict[
    tuple[WorkstreamStatus, WorkstreamStatus], StatusEffect
] = {
    (WorkstreamStatus.FAILED, WorkstreamStatus.READY): StatusEffect(
        event=EventType.WORKSTREAM_RETRYING
    ),
    (WorkstreamStatus.NEEDS_REVIEW, WorkstreamStatus.READY): StatusEffect(
        event=EventType.WORKSTREAM_APPROVED
    ),
}
```

> The real-import block replaces the `TYPE_CHECKING` guard from Task 1 for
> `EventType`/`NotificationEvent` (the tables need the values at runtime). This is
> safe: `event_log` and `notifications.base` do not import `transitions`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transitions.py -q`
Expected: PASS. (If totality fails, a status is missing an explicit entry — add
`StatusEffect()`, do not leave it out.)

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format maestro/transitions.py tests/test_transitions.py && uv run ruff check maestro/transitions.py tests/test_transitions.py && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/transitions.py tests/test_transitions.py
git commit -m "feat(transitions): total per-entity effect + override tables"
```

---

## Task 5: `TransitionDispatcher`

**Files:**
- Modify: `maestro/transitions.py`
- Test: `tests/test_transitions.py`

**Interfaces:**
- Consumes: the tables (Task 4), `Event`/`EventLogger` (`event_log`), `Notification`/`NotificationManager`, `StatusChangeCallback`.
- Produces: `TransitionDispatcher(notifier, event_logger_getter, status_change_cb)` with `async def fire(subject, *, frm, details=None, message=None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transitions.py  (add)
import asyncio
import pytest
from maestro.models import TaskStatus, TransitionSubject
from maestro.transitions import TransitionDispatcher


class _Rec:
    def __init__(self): self.events=[]; self.notifs=[]; self.cb=[]
    def logger_getter(self):
        rec = self
        class L:
            def log(self, ev): rec.events.append(ev)
        return L()
    async def notify(self, n): self.notifs.append(n)
    def on_change(self, i, f, t): self.cb.append((i, f, t))


def _disp(rec):
    return TransitionDispatcher(
        notifier=type("N", (), {"notify": staticmethod(rec.notify)})(),
        event_logger_getter=rec.logger_getter,
        status_change_cb=rec.on_change,
    )


async def test_same_state_fires_nothing():
    rec=_Rec(); d=_disp(rec)
    s=TransitionSubject("task","t","T",TaskStatus.RUNNING)
    await d.fire(s, frm=TaskStatus.RUNNING)  # frm == to
    assert not rec.events and not rec.notifs and not rec.cb


async def test_empty_effect_fires_only_callback():
    rec=_Rec(); d=_disp(rec)
    s=TransitionSubject("task","t","T",TaskStatus.VALIDATING)  # empty effect
    await d.fire(s, frm=TaskStatus.RUNNING)
    assert rec.cb == [("t", "running", "validating")]  # STRINGS, not enums
    assert not rec.events and not rec.notifs


async def test_full_effect_fires_all_three():
    rec=_Rec(); d=_disp(rec)
    s=TransitionSubject("task","t","T",TaskStatus.RUNNING)
    await d.fire(s, frm=TaskStatus.READY)
    assert rec.cb == [("t", "ready", "running")]
    assert len(rec.events)==1 and len(rec.notifs)==1


async def test_override_beats_entry_table():
    rec=_Rec(); d=_disp(rec)
    s=TransitionSubject("task","t","T",TaskStatus.READY)
    await d.fire(s, frm=TaskStatus.FAILED)  # retry override -> TASK_RETRYING event
    from maestro.event_log import EventType
    assert rec.events[0].event_type == EventType.TASK_RETRYING


async def test_subscriber_exception_isolated():
    rec=_Rec()
    def boom(*a): raise RuntimeError("cb down")
    d=TransitionDispatcher(
        notifier=type("N", (), {"notify": staticmethod(rec.notify)})(),
        event_logger_getter=rec.logger_getter, status_change_cb=boom,
    )
    s=TransitionSubject("task","t","T",TaskStatus.RUNNING)
    await d.fire(s, frm=TaskStatus.READY)  # must NOT raise
    assert len(rec.events)==1 and len(rec.notifs)==1  # other sinks still fired


async def test_cancelled_error_reraised():
    def cancel(*a): raise asyncio.CancelledError()
    d=TransitionDispatcher(notifier=None, event_logger_getter=lambda: None,
                           status_change_cb=cancel)
    s=TransitionSubject("task","t","T",TaskStatus.RUNNING)
    with pytest.raises(asyncio.CancelledError):
        await d.fire(s, frm=TaskStatus.READY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transitions.py -q -k dispatcher or fire`
Expected: FAIL — `TransitionDispatcher` missing.

- [ ] **Step 3: Implement**

```python
# maestro/transitions.py  (add)
import asyncio
import logging

from maestro.event_log import Event

_logger = logging.getLogger(__name__)


def _effect_for(subject, frm):
    if subject.kind == "task":
        overrides, entries = TASK_TRANSITION_OVERRIDES, TASK_EFFECTS
    else:
        overrides, entries = WORKSTREAM_TRANSITION_OVERRIDES, WORKSTREAM_EFFECTS
    return overrides.get((frm, subject.status)) or entries[subject.status]


class TransitionDispatcher:
    """Fires the declarative effect for a committed transition (best-effort)."""

    def __init__(self, *, notifier, event_logger_getter, status_change_cb):
        self._notifier = notifier
        self._event_logger_getter = event_logger_getter
        self._status_change_cb = status_change_cb

    async def fire(self, subject, *, frm, details=None, message=None) -> None:
        if frm == subject.status:  # not a transition
            return
        effect = _effect_for(subject, frm)
        # 1) status callback — every real transition, plain strings
        await self._run(self._callback, subject, frm)
        # 2) event log
        if effect.event is not None:
            await self._run(self._log, subject, effect, message, details)
        # 3) notification
        if effect.notification is not None:
            await self._run(self._notify, subject, effect, message)

    async def _run(self, fn, *args) -> None:
        try:
            res = fn(*args)
            if asyncio.iscoroutine(res):
                await res
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("transition subscriber failed (swallowed)")

    def _callback(self, subject, frm):
        if self._status_change_cb is not None:
            self._status_change_cb(subject.id, frm.value, subject.status.value)

    def _log(self, subject, effect, message, details):
        logger = self._event_logger_getter()
        if logger is None:
            return
        logger.log(Event(
            event_type=effect.event,
            entity_type=subject.kind,
            entity_id=subject.id if subject.kind == "workstream" else None,
            task_id=subject.id if subject.kind == "task" else None,
            message=message,
            details=details or {},
        ))

    async def _notify(self, subject, effect, message):
        if self._notifier is None:
            return
        from maestro.notifications.base import Notification
        await self._notifier.notify(
            Notification.from_subject(subject, effect.notification, message)
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transitions.py -q`
Expected: PASS (all dispatcher + table + type tests).

- [ ] **Step 5: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add maestro/transitions.py tests/test_transitions.py
git commit -m "feat(transitions): TransitionDispatcher (kind-select, contract, fail-isolation)"
```

---

## Task 6: Scheduler wiring

**Files:**
- Modify: `maestro/scheduler.py`
- Test: `tests/test_scheduler.py`, `tests/test_transition_guard.py`

**Interfaces:**
- Consumes: `TransitionDispatcher`, `TransitionSubject`, the tables, `reset_for_retry_atomic`.
- Produces: `Scheduler._transition(task_id, to_status, *, expected_status, details=None, message=None, **fields) -> Task`; `Scheduler._dispatch_committed_transition(task, *, frm) -> None`. `self._dispatcher` built in `__init__` from the existing subscribers (`self._notifications`, `get_event_logger`, `self._on_status_change`).

- [ ] **Step 1: Write the failing contract tests**

Model on existing scheduler tests (find them: `grep -n "TASK_STARTED\|_emit_event\|update_task_status" tests/test_scheduler.py`). Assert the NEW contract:

```python
# tests/test_scheduler.py  (add — adapt construction to the existing harness)
# - entering RUNNING fires TASK_STARTED event AND notification (before launch);
# - entering FAILED fires TASK_FAILED event and NO notification;
# - entering DONE fires TASK_COMPLETED event + notification;
# - entering NEEDS_REVIEW fires TASK_NEEDS_REVIEW event + notification;
# - reset_for_retry_atomic success fires TASK_RETRYING (built from the re-read task);
# - reset_for_retry_atomic ok=False fires nothing;
# - the status-change callback still receives ("id","<frm>","<to>") strings.
```

Use a fake notifier + a captured event logger (the repo's event-log test helper)
and assert the recorded events/notifications per transition.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_scheduler.py -q -k transition`
Expected: FAIL (new contract not wired; e.g. no TASK_STARTED event today).

- [ ] **Step 3: Implement**

In `Scheduler.__init__`, build the dispatcher:

```python
from maestro.transitions import TransitionDispatcher, TransitionSubject  # subject via models
from maestro.event_log import get_event_logger

self._dispatcher = TransitionDispatcher(
    notifier=self._notifications,
    event_logger_getter=get_event_logger,
    status_change_cb=self._on_status_change,
)
```

Add the primitives:

```python
    async def _transition(self, task_id, to_status, *, expected_status,
                          details=None, message=None, **fields):
        task = await self._db.update_task_status(
            task_id, to_status, expected_status=expected_status, **fields)
        await self._dispatcher.fire(
            _subject(task), frm=expected_status, details=details, message=message)
        return task

    async def _dispatch_committed_transition(self, task, *, frm):
        await self._dispatcher.fire(_subject(task), frm=frm)
```

where `_subject(task) = TransitionSubject("task", task.id, task.title, task.status)`.

Now migrate the ~15 status sites:
- Each `update_task_status(id, S, expected_status=E)` that is a real transition →
  `await self._transition(id, S, expected_status=E, message=..., details=...)`,
  and DELETE the adjacent `_emit_event`/`_notify` calls the table now covers.
- The launch-path `_notify(TASK_STARTED)` after `backend.run()` (scheduler.py:1049)
  is DELETED — `TASK_STARTED` now fires from the `RUNNING` `_transition` (§0).
- `reset_for_retry_atomic(...)` sites (575/1319/1420): on `ok is True`, re-read
  the task (`task = await self._db.get_task(task_id)`) and call
  `await self._dispatch_committed_transition(task, frm=TaskStatus.FAILED)`.
- LEAVE arbiter/validation/tick/lifecycle `_emit_event` and the `TASK_TIMEOUT`
  `_notify` at their sites (not transitions).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_scheduler.py -q`
Expected: PASS. Update any existing test that asserted the OLD emit/notify timing
to the new contract (this is the documented behavior change — do not weaken, just
re-point the expectation) and note it in the commit.

- [ ] **Step 5: Add the AST guard (scheduler rules)**

```python
# tests/test_transition_guard.py
import ast, pathlib


def _calls_inside(tree, method, allowed_enclosing):
    bad = []
    class V(ast.NodeVisitor):
        def __init__(self): self.stack=[]
        def visit_FunctionDef(self, n): self.stack.append(n.name); self.generic_visit(n); self.stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Call(self, n):
            f=n.func
            if isinstance(f, ast.Attribute) and f.attr==method:
                if not (self.stack and self.stack[-1] in allowed_enclosing):
                    bad.append((self.stack[-1] if self.stack else "<module>", n.lineno))
            self.generic_visit(n)
    V().visit(tree); return bad


def test_scheduler_update_task_status_only_in_transition():
    tree = ast.parse(pathlib.Path("maestro/scheduler.py").read_text())
    assert _calls_inside(tree, "update_task_status", {"_transition"}) == []


def test_transitions_not_imported_by_database():
    src = pathlib.Path("maestro/database.py").read_text()
    assert "import transitions" not in src and "from maestro.transitions" not in src
```

Run: `uv run pytest tests/test_transition_guard.py -q`
Expected: PASS.

- [ ] **Step 6: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 7: Commit**

```bash
git add maestro/scheduler.py tests/test_scheduler.py tests/test_transition_guard.py
git commit -m "feat(scheduler): route transitions through dispatcher (behavior change per spec §0)"
```

---

## Task 7: Orchestrator wiring

**Files:**
- Modify: `maestro/orchestrator.py`
- Test: `tests/test_orchestrator.py`, `tests/test_transition_guard.py`

**Interfaces:**
- Consumes: `TransitionDispatcher`, `TransitionSubject`, `get_event_logger`.
- Produces: `Orchestrator.__init__` gains `notifier: NotificationManager | None = None`, `on_status_change: StatusChangeCallback | None = None`; `Orchestrator._transition(id, to, *, expected_status, details=None, message=None, **fields)`; `Orchestrator._update_fields(id, **fields)`.

- [ ] **Step 1: Write the failing tests**

Reuse the orchestrator harness (`tests/test_orchestrator.py:176` fixture). Assert:

```python
# tests/test_orchestrator.py  (add)
# - workstream RUNNING transition fires WORKSTREAM_RUNNING event + WORKSTREAM_STARTED notif;
# - DONE fires WORKSTREAM_DONE + WORKSTREAM_COMPLETED notif;
# - FAILED fires WORKSTREAM_FAILED event + WORKSTREAM_FAILED notif;
# - NEEDS_REVIEW fires event + notif;
# - PR_CREATED and MERGING fire events but NO notification;
# - _update_fields (generation_pid clear) fires nothing.
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -q -k "transition or workstream_event"`
Expected: FAIL — orchestrator emits nothing today.

- [ ] **Step 3: Implement**

Add subscribers to `__init__` (after `self._config = config`):

```python
from maestro.transitions import TransitionDispatcher, TransitionSubject
from maestro.event_log import get_event_logger

self._notifier = notifier
self._dispatcher = TransitionDispatcher(
    notifier=notifier,
    event_logger_getter=get_event_logger,
    status_change_cb=on_status_change,
)
```

Add `_transition` / `_update_fields` (mirroring Task 6, with
`update_workstream_status` and `_subject = TransitionSubject("workstream",
ws.id, ws.title, ws.status)`; `_update_fields` calls
`update_workstream_status(id, <same status>, **fields)` and does NOT dispatch).

Migrate the ~35 sites: real transitions → `_transition`; the same-state
`generation_pid` clear (orchestrator.py:550) and any other pure column patch →
`_update_fields`; the atomic CLI approval path is NOT touched (out of scope, §2).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: PASS. Adjust any success-path fixture that now emits unexpectedly only
if it was asserting "no events" — the new events are intended.

- [ ] **Step 5: Extend the AST guard (orchestrator rules)**

```python
# tests/test_transition_guard.py  (add)
def test_orchestrator_update_workstream_status_only_in_transition_or_fields():
    import ast, pathlib
    tree = ast.parse(pathlib.Path("maestro/orchestrator.py").read_text())
    assert _calls_inside(
        tree, "update_workstream_status", {"_transition", "_update_fields"}
    ) == []
```

Run: `uv run pytest tests/test_transition_guard.py -q`
Expected: PASS.

- [ ] **Step 6: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 7: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py tests/test_transition_guard.py
git commit -m "feat(orchestrator): workstream transition events + notifications via dispatcher"
```

---

## Task 8: Verification & PR

- [ ] **Step 1: Full green gate**

Run: `uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check`
Expected: all pass, 0 pyrefly errors.

- [ ] **Step 2: CLI dashboard-callback smoke**

The CLI `_on_status_change` (cli.py:539) compares `new_status == "running"/"done"`.
Run a short scheduler flow (or the existing CLI test path) and confirm the
callback still receives strings and the dashboard status still updates.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/transition-hooks
gh pr create --base master --title "feat: transition hooks (idea #10)" \
  --body "<summary; link spec 2026-07-23-maestro-transition-hooks-design.md; call out the §0 behavior change in the changelog section>"
```

The PR description MUST include the behavior-change note (moved `TASK_STARTED`,
new task lifecycle events, task/workstream `FAILED` notification asymmetry,
mode-2 events+notifications) so the change is visible in the changelog.

- [ ] **Step 4: Read Copilot review**

Address valid inline comments with follow-up commits; reply with rationale to
invalid ones. Do not merge (the user merges).

---

## Self-Review (plan vs spec)

- §3.1 `TransitionSubject` (neutral module) → Task 1.
- §3.1 `StatusEffect` → Task 1.
- §3.2 Event envelope + serialization → Task 2. §6 `WORKSTREAM_*` events → Task 2.
- §3.2 Notification generalization + `from_subject` adapters → Task 3. §6 four notifications → Task 3.
- §5.1 totality, §5.2 what belongs, §5.3 task table, §5.4 split override tables → Task 4.
- §3.3 dispatcher (kind-select, subscriber truth table, string callback), §3.3.2 fail-isolation → Task 5.
- §4.1 `_transition` (CAS frm, keyword-only details/message), §4.3 committed-transition + re-read, §7 scheduler wiring, §0 behavior changes → Task 6.
- §4.2 `_update_fields`, §6 orchestrator wiring, mode-2 subscribers → Task 7.
- §9 AST guard (enclosing-def rule + `database` import rule + retry ok=True/False behavior) → Tasks 6 (scheduler + import rule + retry) & 7 (orchestrator rule).
- §9 envelope back-compat → Task 2 test. §10 out-of-scope (CLI/REST/MCP firing, outbox, enum unification) → not implemented; the CLI approval path is explicitly left untouched in Task 7.
