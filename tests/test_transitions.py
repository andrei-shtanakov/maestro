from maestro.event_log import EventType
from maestro.models import TaskStatus, TransitionSubject, WorkstreamStatus
from maestro.notifications.base import NotificationEvent
from maestro.transitions import (
    TASK_EFFECTS,
    TASK_TRANSITION_OVERRIDES,
    WORKSTREAM_EFFECTS,
    WORKSTREAM_TRANSITION_OVERRIDES,
    StatusEffect,
)


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
    assert (
        TASK_TRANSITION_OVERRIDES[(TaskStatus.FAILED, TaskStatus.READY)].event
        == EventType.TASK_RETRYING
    )
    assert (
        WORKSTREAM_TRANSITION_OVERRIDES[
            (WorkstreamStatus.FAILED, WorkstreamStatus.READY)
        ].event
        == EventType.WORKSTREAM_RETRYING
    )
