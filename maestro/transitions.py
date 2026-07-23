"""Declarative status-transition -> side-effect mapping and dispatcher."""

from __future__ import annotations

from dataclasses import dataclass

from maestro.event_log import EventType
from maestro.models import TaskStatus, WorkstreamStatus
from maestro.notifications.base import NotificationEvent


@dataclass(frozen=True)
class StatusEffect:
    """The side effect of entering (or transitioning into) a status."""

    event: EventType | None = None
    notification: NotificationEvent | None = None


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
