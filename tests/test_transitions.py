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
