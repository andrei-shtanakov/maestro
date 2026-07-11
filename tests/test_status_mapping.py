"""Tests for TaskStatus → TaskOutcomeStatus mapping used by recovery."""

import pytest

from maestro.coordination.routing import (
    interrupted_error_code,
    task_status_to_outcome_status,
)
from maestro.models import TaskOutcomeStatus, TaskStatus


class TestMapping:
    def test_done_maps_to_success(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.DONE) is TaskOutcomeStatus.SUCCESS
        )

    def test_failed_maps_to_failure(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.FAILED)
            is TaskOutcomeStatus.FAILURE
        )

    def test_needs_review_maps_to_failure(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.NEEDS_REVIEW)
            is TaskOutcomeStatus.FAILURE
        )

    def test_abandoned_maps_to_cancelled(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.ABANDONED)
            is TaskOutcomeStatus.CANCELLED
        )

    def test_running_maps_to_cancelled_with_interrupted_marker(self) -> None:
        """Interrupted runs go on the wire as CANCELLED (#65) + error_code."""
        assert (
            task_status_to_outcome_status(TaskStatus.RUNNING)
            is TaskOutcomeStatus.CANCELLED
        )
        assert interrupted_error_code(TaskStatus.RUNNING) == "interrupted"

    def test_validating_maps_to_cancelled_with_interrupted_marker(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.VALIDATING)
            is TaskOutcomeStatus.CANCELLED
        )
        assert interrupted_error_code(TaskStatus.VALIDATING) == "interrupted"

    def test_terminal_states_carry_no_interrupted_marker(self) -> None:
        for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ABANDONED):
            assert interrupted_error_code(status) is None

    def test_every_mapped_status_is_in_arbiter_enum(self) -> None:
        """No lifecycle state may project outside the contract enum (#65)."""
        wire = {"success", "failure", "timeout", "cancelled"}
        for status in TaskStatus:
            mapped = task_status_to_outcome_status(status)
            assert mapped is None or mapped.value in wire

    @pytest.mark.parametrize(
        "invariant_state",
        [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.AWAITING_APPROVAL],
    )
    def test_invariant_violation_states_return_none(
        self, invariant_state: TaskStatus
    ) -> None:
        assert task_status_to_outcome_status(invariant_state) is None
