"""Persistent local transfer queue for Windows sender.

Stores pending and active outbound transfer jobs in SQLite so queued transfers survive
reboots, network interruptions, and application restarts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from homevault_protocol.database import TransferDatabase
from homevault_protocol.hashing import hash_file
from homevault_protocol.models import (
    Platform,
    TransferJob,
    TransferMode,
    TransferState,
    validate_platform,
)
from homevault_protocol.state_machine import TransferStateMachine

logger = logging.getLogger(__name__)


class QueueError(Exception):
    """Raised when an invalid queue operation is requested."""


class SenderQueue:
    """Manages the persistent SQLite queue of file transfers on Windows."""

    def __init__(self, database: TransferDatabase) -> None:
        self.database = database
        self.database.migrate()

    def enqueue_file(
        self,
        file_path: str | Path,
        mode: TransferMode = TransferMode.COPY,
        target_platform: Platform | str = Platform.MACOS,
    ) -> TransferJob:
        """Enqueue a local Windows file for transfer to the Mac receiver.

        Validates the path, records initial metadata, and persists a
        TransferJob with state PENDING_UPLOAD in SQLite.

        Args:
            file_path: Absolute or relative path to the source file.
            mode: TransferMode.COPY or TransferMode.MOVE.
            target_platform: Target destination platform (default: Platform.MACOS).

        Returns:
            The newly created and persisted TransferJob.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the path is a directory or not a regular file.
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")
        if path.is_dir():
            raise ValueError(
                f"HomeVault transfers regular files; directories are not supported: {path}"
            )
        if not path.is_file():
            raise ValueError(f"Path is not a regular file: {path}")

        file_size = path.stat().st_size
        sha256 = hash_file(path)

        target = validate_platform(target_platform, "target_platform")
        if target == Platform.WINDOWS:
            raise ValueError("Target platform cannot be Windows for transfers sent from Windows")

        job = TransferJob(
            mode=mode,
            state=TransferState.PENDING_UPLOAD,
            source_path=str(path),
            filename=path.name,
            file_size=file_size,
            sha256_hash=sha256,
            sender_platform=Platform.WINDOWS,
            target_platform=target,
        )

        self.database.insert(job)
        logger.info(
            "Enqueued Windows job %s (%s, %s, %d bytes) -> %s",
            job.transfer_id,
            job.filename,
            job.mode.value,
            job.file_size,
            job.target_platform.value,
        )
        return job

    def get_job(self, transfer_id: str) -> Optional[TransferJob]:
        """Fetch a specific job by transfer_id."""
        return self.database.get(transfer_id)

    def get_pending_uploads(self) -> list[TransferJob]:
        """Return all outbound jobs requiring upload (PENDING_UPLOAD or interrupted UPLOADING)."""
        pending = self.database.get_by_sender(Platform.WINDOWS, TransferState.PENDING_UPLOAD)
        uploading = self.database.get_by_sender(Platform.WINDOWS, TransferState.UPLOADING)
        return pending + uploading

    def get_queued_transfers(self) -> list[TransferJob]:
        """Return all staged outbound jobs awaiting Mac receiver completion (state QUEUED)."""
        return self.database.get_by_sender(Platform.WINDOWS, TransferState.QUEUED)

    def get_all_jobs(self, limit: int = 50) -> list[TransferJob]:
        """Return all Windows-originated jobs ordered by creation time."""
        jobs = self.database.get_by_sender(Platform.WINDOWS)
        return jobs[-limit:] if len(jobs) > limit else jobs

    def cancel_job(self, transfer_id: str) -> bool:
        """Cancel a pending or queued job.

        Returns:
            True if cancelled, False if job not found or already terminal.
        """
        job = self.database.get(transfer_id)
        if not job:
            return False
        if TransferStateMachine.is_terminal(job.state):
            logger.warning(
                "Cannot cancel terminal job %s (state: %s)", transfer_id, job.state.value
            )
            return False

        TransferStateMachine.transition(job, TransferState.CANCELLED)
        self.database.update(job)
        logger.info("Cancelled transfer %s", transfer_id)
        return True

    def retry_job(self, transfer_id: str) -> bool:
        """Re-queue a failed or needs-attention job for re-transmission.

        Returns:
            True if reset to PENDING_UPLOAD, False otherwise.
        """
        job = self.database.get(transfer_id)
        if not job:
            return False
        if job.state not in (TransferState.FAILED, TransferState.NEEDS_ATTENTION):
            logger.warning(
                "Job %s is in state %s; only FAILED or NEEDS_ATTENTION can be retried.",
                transfer_id,
                job.state.value,
            )
            return False

        # Reset retry counter and error message
        job.retry_count = 0
        job.error_message = None
        TransferStateMachine.transition(job, TransferState.PENDING_UPLOAD)
        self.database.update(job)
        logger.info("Retried transfer %s (reset to PENDING_UPLOAD)", transfer_id)
        return True
