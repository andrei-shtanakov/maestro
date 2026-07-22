from maestro.gate_approvals import (
    ApprovalMarker,
    build_approval_marker,
    parse_approval_marker,
)


def test_build_then_parse_roundtrips():
    marker = build_approval_marker("ex_post", "abc1234")
    assert marker == "gates:approval-required phase=ex_post sha=abc1234"
    parsed = parse_approval_marker(f"scope escape: a.py; re-queue to approve. {marker}")
    assert parsed == ApprovalMarker(phase="ex_post", sha="abc1234")


def test_parse_returns_none_without_marker():
    assert parse_approval_marker("scope escape: a.py") is None


def test_gates_still_reexports_primitives():
    # Backward-compat: existing import sites use maestro.gates
    from maestro.gates import APPROVAL_MARKER_PREFIX
    from maestro.gates import parse_approval_marker as pg

    assert APPROVAL_MARKER_PREFIX == "gates:approval-required"
    marker = pg("x gates:approval-required phase=ex_ante sha=deadbeef")
    assert marker is not None
    assert marker.phase == "ex_ante"
