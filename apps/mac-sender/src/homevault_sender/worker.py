"""macOS sender background queue worker daemon.

Processes pending file uploads, stages encrypted payloads to Google Drive,
and monitors completion receipts to safely finalize Copy and Move jobs.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import keyring

from homevault_protocol.config import SenderConfig
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.metadata import TransferMetadata
from homevault_protocol.models import TransferJob, TransferMode, TransferState
from homevault_protocol.state_machine import TransferStateMachine
from homevault_sender.notifications import send_notification
from homevault_sender.queue import SenderQueue
from homevault_sender.receiver import MacReceiver
from homevault_sender.staging import StagingManager

logger = logging.getLogger("homevault.sender")


class SenderWorker:
    """Background worker daemon for macOS file uploads, receipt finalization, and incoming transfers."""

    def __init__(
        self,
        config: SenderConfig,
        drive_client: DriveClient,
        database: TransferDatabase,
        queue: Optional[SenderQueue] = None,
        staging_manager: Optional[StagingManager] = None,
        encryption_key: Optional[str] = None,
        receiver: Optional[MacReceiver] = None,
        receiver_db: Optional[TransferDatabase] = None,
        enable_receiver: bool = True,
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

        if receiver:
            self.receiver = receiver
        elif enable_receiver:
            rec_db_path = Path(
                getattr(self.config, "receiver_db_path", None)
                or (Path(self.config.db_path).parent / "receiver.db")
            ).expanduser()
            rec_db_path.parent.mkdir(parents=True, exist_ok=True)
            self._receiver_db = receiver_db or TransferDatabase(rec_db_path)
            self.receiver = MacReceiver(
                drive_client=self.drive_client,
                inbox_dir=getattr(self.config, "inbox_path", None),
                database=self._receiver_db,
                config=self.config,
                encryption_key=self._explicit_key,
            )
        else:
            self.receiver = None


    def get_encryption_key(self) -> str:
        """Retrieve the shared encryption key from macOS Keychain or environment."""
        if self._explicit_key:
            return self._explicit_key

        env_key = os.environ.get("HOMEVAULT_ENCRYPTION_KEY")
        if env_key:
            return env_key.strip()

        stored_key = keyring.get_password("HomeVault", "encryption-key")
        if stored_key:
            return stored_key.strip()

        raise RuntimeError(
            "HomeVault encryption key not found in macOS Keychain or environment.\n"
            "Please store your encryption key first with:\n"
            '  security add-generic-password -s "HomeVault" -a "encryption-key" -w "<KEY>" -U'
        )

    def process_pending_uploads(self) -> int:
        """Upload any jobs waiting in PENDING_UPLOAD or interrupted UPLOADING state."""
        pending = self.queue.get_pending_uploads()
        if not pending:
            return 0

        logger.info("Found %d pending upload(s). Processing...", len(pending))
        key = self.get_encryption_key()
        uploaded_count = 0

        for job in pending:
            if not self._running and self._is_looping:
                break

            logger.info("Staging job %s (%s, %d bytes)", job.transfer_id, job.filename, job.file_size)
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
                        TransferStateMachine.transition(job, TransferState.FAILED, error_message=str(err))
                    except Exception:
                        job.state = TransferState.FAILED
                        job.error_message = str(err)
                    self.database.update(job)
                    send_notification(
                        title="HomeVault — Upload Failed",
                        message=f"Failed to upload '{job.filename}': max retries exceeded.",
                    )
                else:
                    # Keep in PENDING_UPLOAD to retry on next cycle
                    try:
                        TransferStateMachine.transition(job, TransferState.PENDING_UPLOAD, error_message=str(err))
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
                    continue  # Still waiting for Windows receiver to download

                logger.info("Received completion receipt for transfer %s", job.transfer_id)

                if job.mode == TransferMode.MOVE:
                    success = self.staging_manager.finalize_move(job, receipt, delete_cloud_staging=True)
                    if success:
                        receipted_count += 1
                        send_notification(
                            title="HomeVault — Move Completed",
                            message=f"'{job.filename}' delivered to Windows Inbox. Original deleted.",
                        )
                    else:
                        send_notification(
                            title="HomeVault Alert",
                            message=f"Source '{job.filename}' was modified after staging. Original retained.",
                        )
                else:
                    # COPY operation: verify receipt against metadata
                    metadata = TransferMetadata.from_job(job)
                    if not receipt.matches_metadata(metadata):
                        logger.error("Receipt validation failed for copy job %s: metadata mismatch", job.transfer_id)
                        job.state = TransferState.NEEDS_ATTENTION
                        job.error_message = "Completion receipt does not match transfer metadata"
                        self.database.update(job)
                        send_notification(
                            title="HomeVault Alert",
                            message=f"Received invalid completion receipt for '{job.filename}'.",
                        )
                        continue

                    # Commit completed and purge cloud blob
                    TransferStateMachine.transition(job, TransferState.COMPLETED)
                    job.dest_path = receipt.final_path
                    self.database.update(job)

                    if job.drive_file_id:
                        try:
                            self.drive_client.delete_file(job.drive_file_id)
                        except Exception as e:
                            logger.warning("Could not delete cloud staging file %s: %s", job.drive_file_id, e)

                    receipted_count += 1
                    send_notification(
                        title="HomeVault — Copy Delivered",
                        message=f"'{job.filename}' delivered to Windows Inbox.",
                    )

            except Exception as err:
                logger.error("Error checking receipt for job %s: %s", job.transfer_id, err)

        return receipted_count

    def poll_inbound_transfers(self) -> int:
        """Poll and download pending inbound transfers targeted at macOS."""
        if not self.receiver:
            return 0
        try:
            delivered = self.receiver.poll_and_process()
            return len(delivered)
        except Exception as err:
            logger.error("Inbound receiver polling error: %s", err)
            return 0

    def run_once(self) -> dict[str, int]:
        """Execute a single polling iteration for uploads, receipts, and inbound transfers."""
        self._is_looping = False
        uploaded = self.process_pending_uploads()
        receipted = self.check_completion_receipts()
        received = self.poll_inbound_transfers()
        return {"uploaded": uploaded, "receipted": receipted, "received": received}

    def run_forever(self) -> None:
        """Run continuous background queue worker with signal handling."""
        self._running = True
        self._is_looping = True
        self._install_signal_handlers()

        poll_interval = max(5, int(self.config.transfer.poll_interval_seconds))
        logger.info(
            "Starting HomeVault macOS Unified Worker (Poll interval: %ds)",
            poll_interval,
        )

        try:
            while self._running:
                try:
                    self.process_pending_uploads()
                except Exception as err:
                    logger.error("Outbound upload iteration error: %s", err)

                try:
                    self.check_completion_receipts()
                except Exception as err:
                    logger.error("Receipt check iteration error: %s", err)

                try:
                    self.poll_inbound_transfers()
                except Exception as err:
                    logger.error("Inbound receive iteration error: %s", err)

                for _ in range(poll_interval):
                    if not self._running:
                        break
                    time.sleep(1)

        finally:
            logger.info("HomeVault macOS Worker stopped cleanly.")


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
