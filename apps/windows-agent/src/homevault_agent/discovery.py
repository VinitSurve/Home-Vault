"""Windows receiver Google Drive discovery, download, and receipt issuance.

Coordinates:
- Independent discovery of pending transfers by querying HomeVault/Metadata/.
- Deduplication against existing receipts on Google Drive and local SQLite DB.
- Pre-download disk space checks on the Inbox volume.
- Resumable download of encrypted staged payloads.
- Authenticated decryption (AES-256-GCM) and streaming SHA-256 integrity verification.
- Safe path resolution preventing directory traversal and resolving name collisions.
- Same-volume atomic commit to the configured Inbox directory.
- Durable SQLite state transitions and recovery after crashes or reboots.
- Generation and upload of completion receipts to HomeVault/Receipts/.
- Safe post-delivery cleanup of cloud staging blobs.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

from homevault_protocol.crypto import decrypt_file, decrypt_file_streaming
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.filenames import resolve_destination
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt, TransferMetadata
from homevault_protocol.models import TransferJob, TransferMode, TransferState
from homevault_protocol.state_machine import TransferStateMachine

logger = logging.getLogger(__name__)

STREAMING_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB threshold for chunked decryption
DISK_MARGIN_BYTES = 20 * 1024 * 1024  # 20 MB safety buffer above required space


class IntegrityError(Exception):
    """Raised when downloaded file fails SHA-256 checksum or authenticated decryption."""


class InsufficientDiskSpaceError(Exception):
    """Raised when the target drive lacks sufficient free space for download and decryption."""


class CloudDiscoveryManager:
    """Manages Drive polling, download, decryption, and receipting on Windows."""

    def __init__(
        self,
        drive_client: DriveClient,
        inbox_dir: str | Path,
        database: Optional[TransferDatabase] = None,
        temp_dir: Optional[str | Path] = None,
        cleanup_cloud_after_receipt: bool = True,
    ) -> None:
        self.drive_client = drive_client
        self.inbox_dir = Path(inbox_dir).resolve()
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.cleanup_cloud_after_receipt = cleanup_cloud_after_receipt

        # Place staging on the same filesystem as inbox_dir to ensure atomic os.replace
        if temp_dir:
            self.temp_dir = Path(temp_dir).resolve()
        else:
            self.temp_dir = self.inbox_dir / ".homevault_staging"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def check_disk_space(self, required_bytes: int) -> None:
        """Verify that sufficient disk space exists for both encrypted and decrypted files."""
        # Need space for encrypted blob + decrypted payload + safety margin
        total_needed = (required_bytes * 2) + DISK_MARGIN_BYTES
        try:
            usage = shutil.disk_usage(self.inbox_dir)
            if usage.free < total_needed:
                raise InsufficientDiskSpaceError(
                    f"Insufficient disk space on {self.inbox_dir}: "
                    f"requires {total_needed} bytes, only {usage.free} bytes available."
                )
        except OSError as e:
            logger.warning("Could not query disk space on %s: %s", self.inbox_dir, e)

    def discover_pending_transfers(self) -> list[TransferMetadata]:
        """Discover all pending transfers from Drive metadata not yet acknowledged.

        Returns:
            List of TransferMetadata objects awaiting download.
        """
        folder_map = self.drive_client.ensure_folders()

        # 1. Fetch existing receipts on Drive to filter out completed transfers
        receipt_files = self.drive_client.list_files_in_folder(folder_map.receipts_id)
        completed_cloud_ids = {
            f["name"][:-5]
            for f in receipt_files
            if f["name"].endswith(".json")
        }

        # 2. Fetch metadata records from Drive
        metadata_files = self.drive_client.list_files_in_folder(folder_map.metadata_id)
        pending: list[TransferMetadata] = []

        for meta_file in metadata_files:
            if not meta_file["name"].endswith(".json"):
                continue

            transfer_id = meta_file["name"][:-5]
            if transfer_id in completed_cloud_ids:
                continue  # Already finished and confirmed on Drive

            # 3. Check local database for recovery if receipt upload failed previously
            if self.database:
                local_job = self.database.get(transfer_id)
                if local_job and local_job.state == TransferState.COMPLETED:
                    if local_job.dest_path and Path(local_job.dest_path).exists():
                        # File already delivered! Idempotently re-upload receipt to Drive
                        logger.info(
                            "Job %s was completed locally; recovering missing cloud receipt.",
                            transfer_id,
                        )
                        self._upload_receipt_from_local_job(local_job)
                        continue

            try:
                raw_data = self.drive_client.read_json_file(meta_file["id"])
                meta = TransferMetadata(**raw_data)
                pending.append(meta)
            except Exception as err:
                logger.error("Failed to parse metadata file %s: %s", meta_file["name"], err)

        return pending

    def download_and_verify(
        self,
        metadata: TransferMetadata,
        encryption_key: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        dry_run: bool = False,
    ) -> tuple[Optional[Path], Optional[CompletionReceipt]]:
        """Download, decrypt, verify, safely store a transfer, and issue a receipt.

        Args:
            metadata: TransferMetadata for the pending job.
            encryption_key: Base64-encoded AES-256 decryption key.
            progress_callback: Optional callback receiving (bytes_downloaded, total_bytes).
            dry_run: If True, simulate download/decrypt without committing to Inbox or Drive.

        Returns:
            Tuple of (final_destination_path, CompletionReceipt), or (None, None) if dry-run.

        Raises:
            FileNotFoundError: If the staged payload is absent on Drive.
            IntegrityError: If decryption fails or SHA-256 mismatch is detected.
            InsufficientDiskSpaceError: If the destination volume is out of space.
        """
        # 1. Assert sufficient disk space
        self.check_disk_space(metadata.file_size)

        folder_map = self.drive_client.ensure_folders()
        staged_filename = f"{metadata.transfer_id}.enc"
        remote_staged = self.drive_client.find_file(staged_filename, folder_map.staging_id)

        if not remote_staged:
            raise FileNotFoundError(
                f"Staged file {staged_filename} not found on Google Drive in Staging/."
            )

        # 2. Record or update persistent job in local SQLite database (never mutate persistent DB in dry-run)
        job = None if dry_run else self._init_or_update_local_job(metadata)

        enc_path = self.temp_dir / f"{metadata.transfer_id}.enc"
        dec_path = self.temp_dir / f"{metadata.transfer_id}.dec"

        try:
            # 3. Determine if download is required or if we can resume from VERIFYING
            need_download = True
            if job:
                # If already COMPLETED and delivered, return immediately
                if job.state == TransferState.COMPLETED and job.dest_path and Path(job.dest_path).exists():
                    receipt = CompletionReceipt(
                        transfer_id=metadata.transfer_id,
                        final_path=str(job.dest_path),
                        file_size=metadata.file_size,
                        sha256_hash=metadata.sha256_hash,
                        receiver_platform="windows",
                    )
                    return Path(job.dest_path), receipt

                # If in FAILED, reset to QUEUED for retry
                if job.state == TransferState.FAILED:
                    TransferStateMachine.transition(job, TransferState.QUEUED)
                    if self.database:
                        self.database.update(job)

                # If in VERIFYING, check if the staged encrypted file is already intact on disk
                if job.state == TransferState.VERIFYING:
                    if enc_path.exists() and enc_path.stat().st_size > 0:
                        need_download = False
                    else:
                        # Staged file is missing; reset to QUEUED via legal transitions (VERIFYING -> FAILED -> QUEUED)
                        retries = job.retry_count
                        TransferStateMachine.transition(
                            job,
                            TransferState.FAILED,
                            error_message="Previous verification interrupted; restarting download.",
                        )
                        TransferStateMachine.transition(job, TransferState.QUEUED)
                        job.retry_count = retries
                        if self.database:
                            self.database.update(job)

            # 4. Download encrypted payload in chunks with retries (if needed)
            if need_download:
                if job and job.state != TransferState.DOWNLOADING:
                    TransferStateMachine.transition(job, TransferState.DOWNLOADING)
                    if self.database:
                        self.database.update(job)

                self.drive_client.download_file_resumable(
                    file_id=remote_staged["id"],
                    dest_path=enc_path,
                    progress_callback=progress_callback,
                )

                if job and job.state != TransferState.VERIFYING:
                    TransferStateMachine.transition(job, TransferState.VERIFYING)
                    if self.database:
                        self.database.update(job)

            # 6. Authenticated decryption
            try:
                if metadata.file_size > STREAMING_THRESHOLD_BYTES:
                    decrypt_file_streaming(enc_path, dec_path, encryption_key)
                else:
                    decrypt_file(enc_path, dec_path, encryption_key)
            except Exception as err:
                raise IntegrityError(
                    f"Decryption failed for transfer {metadata.transfer_id}: "
                    f"invalid key or corrupted ciphertext. ({err})"
                ) from err

            # 7. SHA-256 integrity verification
            actual_hash = hash_file(dec_path)
            if actual_hash.lower() != metadata.sha256_hash.lower():
                raise IntegrityError(
                    f"SHA-256 integrity mismatch for {metadata.transfer_id}: "
                    f"expected {metadata.sha256_hash}, got {actual_hash}."
                )

            # In dry-run mode, stop here and report verification success without mutating DB or committing to Inbox
            if dry_run:
                logger.info("[Dry Run] Verified transfer %s (%s)", metadata.transfer_id, metadata.filename)
                return None, None

            # 8. Safe destination resolution with traversal prevention & deduplication
            final_path = resolve_destination(self.inbox_dir, metadata.filename)

            # 9. Atomic commit to final Inbox path
            self._commit_to_inbox(dec_path, final_path)

            # 10. Issue completion receipt and upload to Drive
            receipt = CompletionReceipt(
                transfer_id=metadata.transfer_id,
                final_path=str(final_path),
                file_size=metadata.file_size,
                sha256_hash=metadata.sha256_hash,
                receiver_platform="windows",
            )
            receipt_filename = f"{metadata.transfer_id}.json"
            receipt_id = self.drive_client.upload_json_data(
                name=receipt_filename,
                parent_id=folder_map.receipts_id,
                json_content=receipt.to_json(),
            )

            # 11. Mark job COMPLETED in SQLite
            if job:
                TransferStateMachine.transition(job, TransferState.COMPLETED)
                job.dest_path = str(final_path)
                job.drive_receipt_id = receipt_id
                if self.database:
                    self.database.update(job)

            # 12. Cleanup staged encrypted blob from Drive now that receipt is durable
            if self.cleanup_cloud_after_receipt:
                try:
                    self.cleanup_staged_blob(metadata.transfer_id)
                except Exception as e:
                    logger.warning("Could not delete cloud staged blob %s: %s", metadata.transfer_id, e)

            return final_path, receipt

        except Exception as err:
            logger.error("Error processing transfer %s: %s", metadata.transfer_id, err)
            if job:
                job.retry_count += 1
                retries = job.retry_count
                if retries >= job.max_retries:
                    if job.state != TransferState.FAILED:
                        try:
                            TransferStateMachine.transition(job, TransferState.FAILED, error_message=str(err))
                        except Exception:
                            job.state = TransferState.FAILED
                            job.error_message = str(err)
                else:
                    # Reset to QUEUED for retry via legal state machine transitions
                    if job.state == TransferState.VERIFYING:
                        # State machine requires VERIFYING -> FAILED -> QUEUED
                        try:
                            TransferStateMachine.transition(job, TransferState.FAILED, error_message=str(err))
                            TransferStateMachine.transition(job, TransferState.QUEUED)
                        except Exception:
                            job.state = TransferState.QUEUED
                            job.error_message = str(err)
                    elif job.state == TransferState.DOWNLOADING:
                        try:
                            TransferStateMachine.transition(job, TransferState.QUEUED, error_message=str(err))
                        except Exception:
                            job.state = TransferState.QUEUED
                            job.error_message = str(err)
                    else:
                        job.state = TransferState.QUEUED
                        job.error_message = str(err)
                    job.retry_count = retries
                if self.database:
                    self.database.update(job)
            raise

        finally:
            enc_path.unlink(missing_ok=True)
            dec_path.unlink(missing_ok=True)

    def _commit_to_inbox(self, src: Path, dest: Path) -> None:
        """Atomically commit the decrypted file to the final path and set safe permissions."""
        try:
            # Atomic rename (fastest & atomic on same volume)
            os.replace(src, dest)
        except OSError:
            # Fallback across different filesystems
            shutil.move(str(src), str(dest))

        # Ensure file is not executable (standard readable/writable data file)
        try:
            os.chmod(dest, 0o644)
        except (OSError, NotImplementedError):
            pass

    def _init_or_update_local_job(self, metadata: TransferMetadata) -> Optional[TransferJob]:
        """Fetch or initialize a TransferJob in the local database."""
        if not self.database:
            return None

        job = self.database.get(metadata.transfer_id)
        if job is None:
            job = TransferJob(
                transfer_id=metadata.transfer_id,
                mode=TransferMode(metadata.mode),
                state=TransferState.QUEUED,
                filename=metadata.filename,
                file_size=metadata.file_size,
                sha256_hash=metadata.sha256_hash,
            )
            self.database.insert(job)
        return job

    def _upload_receipt_from_local_job(self, job: TransferJob) -> None:
        """Upload a receipt to Drive using metadata from a locally completed job."""
        folder_map = self.drive_client.ensure_folders()
        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path=job.dest_path or "",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            receiver_platform="windows",
        )
        self.drive_client.upload_json_data(
            name=f"{job.transfer_id}.json",
            parent_id=folder_map.receipts_id,
            json_content=receipt.to_json(),
        )
        if self.cleanup_cloud_after_receipt:
            self.cleanup_staged_blob(job.transfer_id)

    def cleanup_staged_blob(self, transfer_id: str) -> bool:
        """Purge the staged encrypted blob from Google Drive after delivery."""
        folder_map = self.drive_client.ensure_folders()
        staged_filename = f"{transfer_id}.enc"
        remote_staged = self.drive_client.find_file(staged_filename, folder_map.staging_id)
        if remote_staged:
            return self.drive_client.delete_file(remote_staged["id"])
        return False
