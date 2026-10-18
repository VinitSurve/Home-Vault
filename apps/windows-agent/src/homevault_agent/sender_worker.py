"""Windows sender background queue worker daemon.

Processes pending outbound file uploads originating from Windows, stages encrypted
payloads to Google Drive with explicit platform routing tags, and monitors completion
receipts from the Mac receiver to safely finalize Copy and Move transfers.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional

import keyring

from homevault_agent.notifications import send_notification
from homevault_agent.queue import SenderQueue
from homevault_agent.staging import StagingManager
from homevault_protocol.config import SenderConfig
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.models import TransferJob, TransferMode, TransferState
from homevault_protocol.state_machine import TransferStateMachine

logger = logging.getLogger("homevault.windows_sender")


class SenderWorker:
    """Background worker daemon for Windows file uploads and receipt finalization."""

    def __init__(
        self,
        config: SenderConfig,
        drive_client: DriveClient,
        database: TransferDatabase,
        queue: Optional[SenderQueue] = None,
        staging_manager: Optional[StagingManager] = None,
        encryption_key: Optional[str] = None,
    ) -> None:
        self.config = config
        self.drive_client = drive_client
        self.database = database
        self.database.migrate()
        self.queue = queue or SenderQueue(database=self.database)
        self.staging_manager = staging_manager or StagingManager(
            drive_client=self.drive_client,
            database=self.database,
        )
        self._explicit_key = encryption_key
        self._running = False
        self._is_looping = False

    def get_encryption_key(self) -> str:
        """Retrieve the shared encryption key from Windows Credential Manager or environment."""
        if self._explicit_key:
            return self._explicit_key

        env_key = os.environ.get("HOMEVAULT_ENCRYPTION_KEY")
        if env_key:
            return env_key.strip()

        stored_key = keyring.get_password("HomeVault", "encryption-key")
        if stored_key:
            return stored_key.strip()

        raise RuntimeError(
            "HomeVault encryption key not found in Windows Credential Manager or environment.\n"
            "Please provision the key securely using:\n"
            '  python -c "import keyring; keyring.set_password(\'HomeVault\', \'encryption-key\', \'<KEY>\')"'
        )

    def process_pending_uploads(self) -> int:
        """Upload any jobs waiting in PENDING_UPLOAD or interrupted UPLOADING state."""
        pending = self.queue.get_pending_uploads()
        if not pending:
            return 0

        logger.info("Found %d pending Windows upload(s). Processing...", len(pending))
        key = self.get_encryption_key()
        uploaded_count = 0

        for job in pending:
            if not self._running and self._is_looping:
                break

            logger.info(
                "Staging job %s (%s, %d bytes) -> Mac",
                job.transfer_id,
                job.filename,
                job.file_size,
            )
            try:
                self.staging_manager.stage_job(job, encryption_key=key)
                uploaded_count += 1
                send_notification(
                    title="HomeVault — Uploaded",
                    message=f"'{job.filename}' staged to cloud queue ({job.mode.value.upper()})",
                )
            except Exception as err:
                logger.error("Upload error for job %s: %s", job.transfer_id, err)
                job.retry_count += 1
                if job.retry_count >= job.max_retries:
                    try:
                        TransferStateMachine.transition(
                            job, TransferState.FAILED, error_message=str(err)
                        )
                    except Exception:
                        job.state = TransferState.FAILED
                        job.error_message = str(err)
                    self.database.update(job)
                    send_notification(
                        title="HomeVault — Upload Failed",
                        message=f"Failed to upload '{job.filename}': max retries exceeded.",
                    )
                else:
                    try:
                        TransferStateMachine.transition(
                            job, TransferState.PENDING_UPLOAD, error_message=str(err)
                        )
                    except Exception:
                        job.state = TransferState.PENDING_UPLOAD
                        job.error_message = str(err)
                    self.database.update(job)

        return uploaded_count

    def check_completion_receipts(self) -> int:
        """Check Google Drive for receipts to finalize QUEUED Move or Copy transfers."""
        queued_jobs = self.queue.get_queued_transfers()
        if not queued_jobs:
            return 0

        receipted_count = 0
        for job in queued_jobs:
            if not self._running and self._is_looping:
                break

            try:
                receipt = self.staging_manager.check_and_fetch_receipt(job.transfer_id)
                if not receipt:
                    continue  # Still waiting for Mac receiver to process

                logger.info("Received completion receipt for transfer %s", job.transfer_id)

                if job.mode == TransferMode.MOVE:
                    success = self.staging_manager.finalize_move(
                        job, receipt, delete_cloud_staging=True
                    )
                    if success:
                        receipted_count += 1
                        send_notification(
                            title="HomeVault — Move Completed",
                            message=f"'{job.filename}' delivered to Mac. Original deleted.",
                        )
                    else:
                        send_notification(
                            title="HomeVault Alert",
                            message=f"Source '{job.filename}' was modified after staging or receipt mismatch. Original retained.",
                        )
                else:
                    # COPY transfer finalization
                    success = self.staging_manager.finalize_copy(
                        job, receipt, delete_cloud_staging=True
                    )
                    if success:
                        receipted_count += 1
                        send_notification(
                            title="HomeVault — Copy Delivered",
                            message=f"'{job.filename}' delivered to Mac.",
                        )
                    else:
                        send_notification(
                            title="HomeVault Alert",
                            message=f"Received invalid completion receipt for '{job.filename}'.",
                        )

            except Exception as err:
                logger.error("Error checking receipt for job %s: %s", job.transfer_id, err)

        return receipted_count

    def run_once(self) -> dict[str, int]:
        """Execute a single polling iteration for uploads and receipts."""
        self._is_looping = False
        uploaded = self.process_pending_uploads()
        receipted = self.check_completion_receipts()
        return {"uploaded": uploaded, "receipted": receipted}

    def run_forever(self) -> None:
        """Run continuous background queue worker with signal handling."""
        self._running = True
        self._is_looping = True
        self._install_signal_handlers()

        poll_interval = max(5, int(self.config.transfer.poll_interval_seconds))
        logger.info(
            "Starting HomeVault Windows Sender Worker (Poll interval: %ds)",
            poll_interval,
        )

        try:
            while self._running:
                try:
                    self.process_pending_uploads()
                    self.check_completion_receipts()
                except Exception as err:
                    logger.error("Unexpected worker iteration error: %s", err)

                for _ in range(poll_interval):
                    if not self._running:
                        break
                    time.sleep(1)

        finally:
            logger.info("HomeVault Windows Sender Worker stopped cleanly.")

    def stop(self) -> None:
        """Signal the worker to stop gracefully."""
        self._running = False

    def _install_signal_handlers(self) -> None:
        """Install graceful shutdown signal handlers."""

        def handle_signal(sig, frame):
            logger.info("Received signal %s. Initiating graceful shutdown...", sig)
            self.stop()

        try:
            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
        except (ValueError, OSError) as e:
            logger.debug("Signal handler registration note: %s", e)
