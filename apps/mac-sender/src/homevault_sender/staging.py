"""macOS sender Google Drive staging and Move finalization interface.

Coordinates:
- Pre-upload Drive storage quota check.
- Client-side authenticated encryption (AES-256-GCM) before upload.
- Resumable upload of encrypted payload to HomeVault/Staging/.
- Metadata upload to HomeVault/Metadata/.
- Safe Move completion: validates receiver receipt, verifies local file content
  still matches the original hash before deleting the original source file.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional

from homevault_protocol.crypto import encrypt_file, encrypt_file_streaming
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt, TransferMetadata
from homevault_protocol.models import TransferJob, TransferMode, TransferState
from homevault_protocol.state_machine import TransferStateMachine

logger = logging.getLogger(__name__)

STREAMING_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB threshold for chunked encryption


class StagingError(Exception):
    """Raised when cloud staging encounters an unrecoverable error."""


class StagingManager:
    """Manages cloud staging operations on the macOS sender."""

    def __init__(
        self,
        drive_client: DriveClient,
        database: Optional[TransferDatabase] = None,
        temp_dir: Optional[Path] = None,
    ) -> None:
        self.drive_client = drive_client
        self.database = database
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir()) / "homevault_sender"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def stage_job(
        self,
        job: TransferJob,
        encryption_key: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> TransferJob:
        """Encrypt, upload, and publish metadata for a transfer job.

        Args:
            job: TransferJob instance with mode, source_path, and file_size populated.
            encryption_key: Base64-encoded AES-256 encryption key.
            progress_callback: Optional callback receiving (bytes_uploaded, total_bytes).

        Returns:
            Updated TransferJob in QUEUED state.
        """
        if not job.source_path or not Path(job.source_path).exists():
            raise FileNotFoundError(f"Source file not found: {job.source_path}")

        source_path = Path(job.source_path)

        # 1. Populate hash and size if not already present
        if not job.sha256_hash:
            job.sha256_hash = hash_file(source_path)
        if not job.file_size:
            job.file_size = source_path.stat().st_size
        if not job.filename:
            job.filename = source_path.name

        enc_path = self.temp_dir / f"{job.transfer_id}.enc"
        try:
            # 2. Check Drive quota before doing heavy encryption or network work
            self.drive_client.check_quota(required_bytes=job.file_size)

            # 3. Transition to UPLOADING
            TransferStateMachine.transition(job, TransferState.UPLOADING)
            if self.database:
                self.database.update(job)
            # 4. Encrypt source file to temporary file
            if job.file_size > STREAMING_THRESHOLD_BYTES:
                encrypt_file_streaming(source_path, enc_path, encryption_key)
            else:
                encrypt_file(source_path, enc_path, encryption_key)

            # 5. Resolve Drive folder tree
            folder_map = self.drive_client.ensure_folders()

            # 6. Idempotent upload: check if already staged
            staged_filename = f"{job.transfer_id}.enc"
            existing_staged = self.drive_client.find_file(staged_filename, folder_map.staging_id)

            if existing_staged:
                job.drive_file_id = existing_staged["id"]
                logger.info("Found existing staged file on Drive (%s), reusing ID.", job.drive_file_id)
            else:
                job.drive_file_id = self.drive_client.upload_file_resumable(
                    local_path=enc_path,
                    remote_name=staged_filename,
                    parent_id=folder_map.staging_id,
                    progress_callback=progress_callback,
                )

            # 7. Upload transfer metadata JSON
            metadata = TransferMetadata.from_job(job)
            meta_filename = f"{job.transfer_id}.json"
            existing_meta = self.drive_client.find_file(meta_filename, folder_map.metadata_id)

            if existing_meta:
                job.drive_metadata_id = existing_meta["id"]
            else:
                job.drive_metadata_id = self.drive_client.upload_json_data(
                    name=meta_filename,
                    parent_id=folder_map.metadata_id,
                    json_content=metadata.to_json(),
                )

            # 8. Transition to QUEUED
            TransferStateMachine.transition(job, TransferState.QUEUED)
            if self.database:
                self.database.update(job)

            return job

        except Exception as err:
            logger.exception("Failed to stage job %s: %s", job.transfer_id, err)
            TransferStateMachine.transition(job, TransferState.FAILED, error_message=str(err))
            if self.database:
                self.database.update(job)
            raise
        finally:
            # Clean up local encrypted temporary file
            enc_path.unlink(missing_ok=True)

    def check_completion(self, job: TransferJob) -> Optional[CompletionReceipt]:
        """Check if the receiver has uploaded a completion receipt for *job*."""
        folder_map = self.drive_client.ensure_folders()
        receipt_filename = f"{job.transfer_id}.json"
        remote_receipt = self.drive_client.find_file(receipt_filename, folder_map.receipts_id)
        if not remote_receipt:
            return None

        receipt_data = self.drive_client.read_json_file(remote_receipt["id"])
        receipt = CompletionReceipt.from_json(json_content=receipt_data if isinstance(receipt_data, str) else None or "")
        return receipt

    def check_and_fetch_receipt(self, transfer_id: str) -> Optional[CompletionReceipt]:
        """Fetch and deserialize a completion receipt by transfer ID."""
        folder_map = self.drive_client.ensure_folders()
        receipt_filename = f"{transfer_id}.json"
        remote_receipt = self.drive_client.find_file(receipt_filename, folder_map.receipts_id)
        if not remote_receipt:
            return None

        raw_dict = self.drive_client.read_json_file(remote_receipt["id"])
        return CompletionReceipt(**raw_dict)

    def finalize_move(
        self,
        job: TransferJob,
        receipt: CompletionReceipt,
        delete_cloud_staging: bool = True,
    ) -> bool:
        """Safely finalize a Move transfer.

        Verifies:
        1. Receipt matches transfer metadata (ID, hash, size).
        2. Source file still exists on disk.
        3. Current SHA-256 hash of source file matches the recorded hash.

        If any condition fails, does NOT delete the source file and marks
        the job as NEEDS_ATTENTION.

        Args:
            job: TransferJob with mode=MOVE.
            receipt: Verified CompletionReceipt from the receiver.
            delete_cloud_staging: Whether to purge the encrypted blob from Drive.

        Returns:
            True if original file was safely deleted.
        """
        if job.mode != TransferMode.MOVE:
            raise ValueError(f"Job {job.transfer_id} is not a Move job (mode: {job.mode}).")

        # 1. Validate receipt against job
        metadata = TransferMetadata.from_job(job)
        if not receipt.matches_metadata(metadata):
            job.state = TransferState.NEEDS_ATTENTION
            job.error_message = "Completion receipt does not match transfer metadata"
            if self.database:
                self.database.update(job)
            return False

        if not job.source_path or not Path(job.source_path).exists():
            job.state = TransferState.NEEDS_ATTENTION
            job.error_message = "Source file was moved or removed before receipt verification"
            if self.database:
                self.database.update(job)
            return False

        # 2. Verify source content has not been modified since transfer started
        current_hash = hash_file(job.source_path)
        if current_hash.lower() != job.sha256_hash.lower():
            job.state = TransferState.NEEDS_ATTENTION
            job.error_message = (
                "Source file was modified after staging. Original retained for safety."
            )
            if self.database:
                self.database.update(job)
            return False

        # 3. Safe deletion of original local file
        Path(job.source_path).unlink()

        # 4. Delete staged encrypted file from Drive if requested
        if delete_cloud_staging and job.drive_file_id:
            try:
                self.drive_client.delete_file(job.drive_file_id)
            except Exception as e:
                logger.warning("Could not delete cloud staged file %s: %s", job.drive_file_id, e)

        # 5. Transition to COMPLETED
        TransferStateMachine.transition(job, TransferState.COMPLETED)
        job.dest_path = receipt.final_path
        if self.database:
            self.database.update(job)

        return True
