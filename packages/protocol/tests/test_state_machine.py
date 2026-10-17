"""Tests for the transfer state machine."""

import pytest

from homevault_protocol.models import TransferJob, TransferMode, TransferState
from homevault_protocol.state_machine import (
    InvalidTransitionError,
    TransferStateMachine,
)


class TestTransitionTable:
    """Verify the transition table is correct and complete."""

    def test_pending_upload_transitions(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.PENDING_UPLOAD)
        assert TransferState.UPLOADING in allowed
        assert TransferState.FAILED in allowed
        assert TransferState.CANCELLED in allowed
        assert TransferState.COMPLETED not in allowed

    def test_uploading_transitions(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.UPLOADING)
        assert TransferState.QUEUED in allowed
        assert TransferState.PENDING_UPLOAD in allowed  # retry
        assert TransferState.FAILED in allowed
        assert TransferState.CANCELLED in allowed

    def test_queued_transitions(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.QUEUED)
        assert TransferState.DOWNLOADING in allowed
        assert TransferState.FAILED in allowed
        assert TransferState.CANCELLED in allowed

    def test_downloading_transitions(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.DOWNLOADING)
        assert TransferState.VERIFYING in allowed
        assert TransferState.QUEUED in allowed  # retry
        assert TransferState.FAILED in allowed

    def test_verifying_transitions(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.VERIFYING)
        assert TransferState.COMPLETED in allowed
        assert TransferState.FAILED in allowed
        assert TransferState.NEEDS_ATTENTION in allowed

    def test_completed_is_terminal(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.COMPLETED)
        assert len(allowed) == 0
        assert TransferStateMachine.is_terminal(TransferState.COMPLETED)

    def test_cancelled_is_terminal(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.CANCELLED)
        assert len(allowed) == 0
        assert TransferStateMachine.is_terminal(TransferState.CANCELLED)

    def test_failed_can_retry(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.FAILED)
        assert TransferState.PENDING_UPLOAD in allowed
        assert TransferState.QUEUED in allowed

    def test_needs_attention_transitions(self):
        allowed = TransferStateMachine.allowed_transitions(TransferState.NEEDS_ATTENTION)
        assert TransferState.PENDING_UPLOAD in allowed
        assert TransferState.CANCELLED in allowed


class TestCanTransition:
    def test_valid_transition(self):
        assert TransferStateMachine.can_transition(
            TransferState.PENDING_UPLOAD, TransferState.UPLOADING
        )

    def test_invalid_transition(self):
        assert not TransferStateMachine.can_transition(
            TransferState.PENDING_UPLOAD, TransferState.COMPLETED
        )

    def test_no_self_transition(self):
        # No state should transition to itself
        for state in TransferState:
            assert not TransferStateMachine.can_transition(state, state), (
                f"{state.value} should not transition to itself"
            )


class TestTransition:
    def test_happy_path_copy(self):
        """Full copy lifecycle: pending → uploading → queued → downloading → verifying → completed."""
        job = TransferJob(mode=TransferMode.COPY)

        TransferStateMachine.transition(job, TransferState.UPLOADING)
        assert job.state == TransferState.UPLOADING

        TransferStateMachine.transition(job, TransferState.QUEUED)
        assert job.state == TransferState.QUEUED

        TransferStateMachine.transition(job, TransferState.DOWNLOADING)
        assert job.state == TransferState.DOWNLOADING

        TransferStateMachine.transition(job, TransferState.VERIFYING)
        assert job.state == TransferState.VERIFYING

        TransferStateMachine.transition(job, TransferState.COMPLETED)
        assert job.state == TransferState.COMPLETED

    def test_happy_path_move(self):
        """Move follows the same lifecycle as Copy (deletion is handled separately)."""
        job = TransferJob(mode=TransferMode.MOVE)

        TransferStateMachine.transition(job, TransferState.UPLOADING)
        TransferStateMachine.transition(job, TransferState.QUEUED)
        TransferStateMachine.transition(job, TransferState.DOWNLOADING)
        TransferStateMachine.transition(job, TransferState.VERIFYING)
        TransferStateMachine.transition(job, TransferState.COMPLETED)
        assert job.state == TransferState.COMPLETED

    def test_invalid_transition_raises(self):
        job = TransferJob()
        with pytest.raises(InvalidTransitionError) as exc_info:
            TransferStateMachine.transition(job, TransferState.COMPLETED)
        assert exc_info.value.current == TransferState.PENDING_UPLOAD
        assert exc_info.value.target == TransferState.COMPLETED

    def test_transition_updates_timestamp(self):
        job = TransferJob()
        original = job.updated_at
        TransferStateMachine.transition(job, TransferState.UPLOADING)
        assert job.updated_at >= original

    def test_transition_with_error_message(self):
        job = TransferJob()
        TransferStateMachine.transition(
            job, TransferState.FAILED, error_message="upload timeout"
        )
        assert job.state == TransferState.FAILED
        assert job.error_message == "upload timeout"

    def test_retry_count_resets_on_progress(self):
        job = TransferJob()
        job.retry_count = 3
        TransferStateMachine.transition(job, TransferState.UPLOADING)
        TransferStateMachine.transition(job, TransferState.QUEUED)
        assert job.retry_count == 0

    def test_cannot_exit_completed(self):
        job = TransferJob(state=TransferState.COMPLETED)
        # Hack state directly to test terminal enforcement
        with pytest.raises(InvalidTransitionError):
            TransferStateMachine.transition(job, TransferState.FAILED)

    def test_cannot_exit_cancelled(self):
        job = TransferJob()
        TransferStateMachine.transition(job, TransferState.CANCELLED)
        with pytest.raises(InvalidTransitionError):
            TransferStateMachine.transition(job, TransferState.PENDING_UPLOAD)

    def test_upload_retry_path(self):
        """Upload interrupted → back to pending_upload → retry."""
        job = TransferJob()
        TransferStateMachine.transition(job, TransferState.UPLOADING)
        TransferStateMachine.transition(job, TransferState.PENDING_UPLOAD)
        TransferStateMachine.transition(job, TransferState.UPLOADING)
        TransferStateMachine.transition(job, TransferState.QUEUED)
        assert job.state == TransferState.QUEUED

    def test_download_retry_path(self):
        """Download interrupted → back to queued → retry."""
        job = TransferJob(state=TransferState.QUEUED)
        TransferStateMachine.transition(job, TransferState.DOWNLOADING)
        TransferStateMachine.transition(job, TransferState.QUEUED)
        TransferStateMachine.transition(job, TransferState.DOWNLOADING)
        TransferStateMachine.transition(job, TransferState.VERIFYING)
        assert job.state == TransferState.VERIFYING

    def test_needs_attention_to_retry(self):
        """Job flagged for review → user retries."""
        job = TransferJob(state=TransferState.VERIFYING)
        TransferStateMachine.transition(job, TransferState.NEEDS_ATTENTION)
        TransferStateMachine.transition(job, TransferState.PENDING_UPLOAD)
        assert job.state == TransferState.PENDING_UPLOAD

    def test_needs_attention_to_cancel(self):
        job = TransferJob(state=TransferState.VERIFYING)
        TransferStateMachine.transition(job, TransferState.NEEDS_ATTENTION)
        TransferStateMachine.transition(job, TransferState.CANCELLED)
        assert job.state == TransferState.CANCELLED


class TestHelpers:
    def test_is_terminal(self):
        assert TransferStateMachine.is_terminal(TransferState.COMPLETED)
        assert TransferStateMachine.is_terminal(TransferState.CANCELLED)
        assert not TransferStateMachine.is_terminal(TransferState.QUEUED)
        assert not TransferStateMachine.is_terminal(TransferState.FAILED)

    def test_is_actionable(self):
        assert TransferStateMachine.is_actionable(TransferState.PENDING_UPLOAD)
        assert TransferStateMachine.is_actionable(TransferState.UPLOADING)
        assert TransferStateMachine.is_actionable(TransferState.QUEUED)
        assert TransferStateMachine.is_actionable(TransferState.DOWNLOADING)
        assert TransferStateMachine.is_actionable(TransferState.VERIFYING)
        assert not TransferStateMachine.is_actionable(TransferState.COMPLETED)
        assert not TransferStateMachine.is_actionable(TransferState.FAILED)
        assert not TransferStateMachine.is_actionable(TransferState.CANCELLED)
        assert not TransferStateMachine.is_actionable(TransferState.NEEDS_ATTENTION)
