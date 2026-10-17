"""Transfer state machine.

Defines the legal state transitions for a transfer job and provides a
validated transition method. No transition can occur outside this table,
ensuring the sender and receiver agree on the protocol.
"""

from __future__ import annotations

from homevault_protocol.models import TransferJob, TransferState


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""

    def __init__(self, current: TransferState, target: TransferState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"Invalid transition: {current.value!r} → {target.value!r}"
        )


# Adjacency map: for each state, the set of states it can transition to.
_TRANSITIONS: dict[TransferState, set[TransferState]] = {
    TransferState.PENDING_UPLOAD: {
        TransferState.UPLOADING,
        TransferState.FAILED,
        TransferState.CANCELLED,
    },
    TransferState.UPLOADING: {
        TransferState.QUEUED,
        TransferState.PENDING_UPLOAD,  # retry after interrupted upload
        TransferState.FAILED,
        TransferState.CANCELLED,
    },
    TransferState.QUEUED: {
        TransferState.DOWNLOADING,
        TransferState.FAILED,
        TransferState.CANCELLED,
    },
    TransferState.DOWNLOADING: {
        TransferState.VERIFYING,
        TransferState.QUEUED,  # retry after interrupted download
        TransferState.FAILED,
        TransferState.CANCELLED,
    },
    TransferState.VERIFYING: {
        TransferState.COMPLETED,
        TransferState.FAILED,
        TransferState.NEEDS_ATTENTION,
    },
    TransferState.COMPLETED: set(),  # terminal state — no transitions out
    TransferState.FAILED: {
        TransferState.PENDING_UPLOAD,  # manual retry from sender
        TransferState.QUEUED,  # manual retry from receiver side
        TransferState.NEEDS_ATTENTION,
    },
    TransferState.CANCELLED: set(),  # terminal state
    TransferState.NEEDS_ATTENTION: {
        TransferState.PENDING_UPLOAD,  # user decides to retry
        TransferState.CANCELLED,  # user decides to abandon
        TransferState.FAILED,
    },
}


class TransferStateMachine:
    """Validates and applies state transitions on TransferJob instances."""

    @staticmethod
    def allowed_transitions(state: TransferState) -> set[TransferState]:
        """Return the set of states reachable from *state*."""
        return _TRANSITIONS.get(state, set())

    @staticmethod
    def can_transition(current: TransferState, target: TransferState) -> bool:
        """Check whether transitioning from *current* to *target* is valid."""
        return target in _TRANSITIONS.get(current, set())

    @classmethod
    def transition(
        cls,
        job: TransferJob,
        target: TransferState,
        *,
        error_message: str | None = None,
    ) -> TransferJob:
        """Move *job* to *target* state, updating timestamps.

        Raises:
            InvalidTransitionError: If the transition is not allowed.

        Returns:
            The same TransferJob instance (mutated in place) for convenience.
        """
        if not cls.can_transition(job.state, target):
            raise InvalidTransitionError(job.state, target)

        job.state = target
        job.touch()

        if error_message is not None:
            job.error_message = error_message

        # Reset retry count on successful forward progress
        if target in (
            TransferState.QUEUED,
            TransferState.VERIFYING,
            TransferState.COMPLETED,
        ):
            job.retry_count = 0

        return job

    @staticmethod
    def is_terminal(state: TransferState) -> bool:
        """Return True if *state* is a terminal (no-exit) state."""
        return state in (TransferState.COMPLETED, TransferState.CANCELLED)

    @staticmethod
    def is_actionable(state: TransferState) -> bool:
        """Return True if *state* requires automatic processing."""
        return state in (
            TransferState.PENDING_UPLOAD,
            TransferState.UPLOADING,
            TransferState.QUEUED,
            TransferState.DOWNLOADING,
            TransferState.VERIFYING,
        )
