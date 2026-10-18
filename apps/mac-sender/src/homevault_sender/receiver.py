"""macOS receiver Google Drive discovery, download, and receipt issuance.

Coordinates:
- Independent discovery of pending transfers targeted at macOS by querying HomeVault/Metadata/.
- Strict platform routing: processes ONLY transfers with target_platform='macos'.
  Legacy transfers (no target_platform) and Windows-targeted transfers are ignored.
- Deduplication against existing receipts on Google Drive and local SQLite DB.
- Pre-download disk space checks on the Inbox volume.
- Resumable download of encrypted staged payloads.
- Authenticated decryption (AES-256) and SHA-256 integrity verification.
- Safe path resolution preventing directory traversal and resolving name collisions.
- Same-volume atomic commit to the configured Inbox directory.
- Generation and upload of completion receipts with receiver_platform='macos' to HomeVault/Receipts/.
- Safe post-delivery cleanup of cloud staging blobs.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

import keyring

from homevault_protocol.config import SenderConfig
from homevault_protocol.crypto import decrypt_file, decrypt_file_streaming
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.filenames import resolve_destination
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt, TransferMetadata
from homevault_protocol.models import Platform, TransferJob, TransferMode, TransferState
from homevault_protocol.state_machine import TransferStateMachine
from homevault_sender.notifications import send_notification

logger = logging.getLogger("homevault.mac_receiver")

STREAMING_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB threshold for chunked decryption
DISK_MARGIN_BYTES = 20 * 1024 * 1024  # 20 MB safety buffer above required space


class IntegrityError(Exception):
    """Raised when downloaded file fails SHA-256 checksum or authenticated decryption."""


class InsufficientDiskSpaceError(Exception):
    """Raised when the target volume lacks sufficient free space for download and decryption."""


class MacReceiver:
    """Manages Drive polling, download, decryption, and receipting on macOS."""

    def __init__(
        self,
        drive_client: DriveClient,
        inbox_dir: Optional[str | Path] = None,
        database: Optional[TransferDatabase] = None,
        temp_dir: Optional[str | Path] = None,
        config: Optional[SenderConfig] = None,
        encryption_key: Optional[str] = None,
        cleanup_cloud_after_receipt: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.drive_client = drive_client
        self.config = config or SenderConfig()
        inbox = inbox_dir or getattr(self.config, "inbox_path", None) or Path.home() / "HomeVault" / "Inbox"
        self.inbox_dir = Path(inbox).resolve()
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.database = database
        if self.database:
            self.database.migrate()
        self.cleanup_cloud_after_receipt = cleanup_cloud_after_receipt
        self.dry_run = dry_run
        self._explicit_key = encryption_key

        # Staging directory on the same filesystem as inbox_dir for atomic os.replace
        if temp_dir:
            self.temp_dir = Path(temp_dir).resolve()
        else:
            self.temp_dir = self.inbox_dir / ".homevault_staging"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

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

    def check_disk_space(self, required_bytes: int) -> None:
        """Verify that sufficient disk space exists for both encrypted and decrypted files."""
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
        """Discover all pending transfers from Drive metadata targeted at macOS.

        Filters out:
        - Transfers with receipts already published to Google Drive.
        - Transfers targeted at Windows (`target_platform="windows"`).
        - Legacy transfers without `target_platform` (which default to Windows).

        Returns:
            List of TransferMetadata objects awaiting download on macOS.
        """
        folder_map = self.drive_client.ensure_folders()

        # 1. Fetch existing receipts on Drive to filter out finished transfers
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
                continue

            # 3. Check local database for recovery if receipt upload failed previously
            if self.database:
                local_job = self.database.get(transfer_id)
                if local_job:
                    if local_job.dest_path and Path(local_job.dest_path).exists():
                        dest_p = Path(local_job.dest_path)
                        if (
                            local_job.state == TransferState.COMPLETED
                            or (
                                dest_p.stat().st_size == local_job.file_size
                                and hash_file(dest_p).lower() == local_job.sha256_hash.lower()
                            )
                        ):
                            logger.info(
                                "Job %s was delivered locally on Mac; recovering missing cloud receipt.",
                                transfer_id,
                            )
                            self._upload_receipt_from_local_job(local_job)
                            continue

            try:
                raw_data = self.drive_client.read_json_file(meta_file["id"])
                meta = TransferMetadata.from_dict(raw_data)

                # Strict routing check: Mac receiver processes ONLY target_platform='macos'
                target = (meta.target_platform or "").strip().lower()
                if target != Platform.MACOS.value:
                    logger.debug(
                        "Mac receiver skipping transfer %s: target_platform is %r (requires %r)",
                        meta.transfer_id,
                        meta.target_platform,
                        Platform.MACOS.value,
                    )
                    continue

                pending.append(meta)
            except Exception as err:
                logger.error("Failed to parse metadata file %s: %s", meta_file["name"], err)

        return pending

    def download_and_verify(
        self,
        metadata: TransferMetadata,
        encryption_key: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        dry_run: Optional[bool] = None,
    ) -> tuple[Optional[Path], Optional[CompletionReceipt]]:
        """Download encrypted payload, decrypt, verify integrity, commit to Inbox, and upload receipt."""
        key = encryption_key or self.get_encryption_key()
        is_dry_run = dry_run if dry_run is not None else self.dry_run

        # 1. Pre-download disk space check
        self.check_disk_space(metadata.file_size)

        folder_map = self.drive_client.ensure_folders()

        # 2. Locate staged encrypted blob on Drive
        staged_name = f"{metadata.transfer_id}.enc"
        remote_blob = self.drive_client.find_file(staged_name, folder_map.staging_id)
        if not remote_blob:
            raise FileNotFoundError(
                f"Staged file {staged_name} not found in HomeVault/Staging/ on Drive."
            )

        enc_path = self.temp_dir / f"{metadata.transfer_id}.download.enc"
        dec_path = self.temp_dir / f"{metadata.transfer_id}.decrypted.tmp"

        job = self._init_or_update_local_job(metadata)

        # Check if already delivered (idempotency recovery before downloading)
        if job and job.dest_path and Path(job.dest_path).exists():
            dest_file = Path(job.dest_path)
            if (
                dest_file.stat().st_size == metadata.file_size
                and hash_file(dest_file).lower() == metadata.sha256_hash.lower()
            ):
                logger.info(
                    "Transfer %s already committed to %s; recovering receipt publication.",
                    metadata.transfer_id,
                    job.dest_path,
                )
                receipt = CompletionReceipt(
                    transfer_id=metadata.transfer_id,
                    final_path=str(job.dest_path),
                    file_size=metadata.file_size,
                    sha256_hash=metadata.sha256_hash,
                    receiver_platform=Platform.MACOS.value,
                )
                receipt_filename = f"{metadata.transfer_id}.json"
                receipt_id = self.drive_client.upload_json_data(
                    name=receipt_filename,
                    parent_id=folder_map.receipts_id,
                    json_content=receipt.to_json(),
                )
                if job.state != TransferState.COMPLETED:
                    try:
                        TransferStateMachine.transition(job, TransferState.COMPLETED)
                    except Exception:
                        job.state = TransferState.COMPLETED
                job.drive_receipt_id = str(receipt_id) if receipt_id is not None else None
                if self.database:
                    self.database.update(job)
                if self.cleanup_cloud_after_receipt:
                    try:
                        self.cleanup_staged_blob(metadata.transfer_id)
                    except Exception as e:
                        logger.warning(
                            "Could not delete cloud staged blob %s: %s",
                            metadata.transfer_id,
                            e,
                        )
                return Path(job.dest_path), receipt

        try:
            # 3. Transition to DOWNLOADING
            if job:
                TransferStateMachine.transition(job, TransferState.DOWNLOADING)
                job.drive_file_id = remote_blob["id"]
                if self.database:
                    self.database.update(job)

            # 4. Resumable download from Drive
            self.drive_client.download_file_resumable(
                file_id=remote_blob["id"],
                dest_path=enc_path,
                progress_callback=progress_callback,
            )

            # 5. Transition to VERIFYING
            if job:
                TransferStateMachine.transition(job, TransferState.VERIFYING)
                if self.database:
                    self.database.update(job)

            # 6. Decrypt payload
            try:
                if metadata.file_size > STREAMING_THRESHOLD_BYTES:
                    decrypt_file_streaming(enc_path, dec_path, key)
                else:
                    decrypt_file(enc_path, dec_path, key)
            except Exception as dec_err:
                raise IntegrityError(
                    f"Decryption failed for {metadata.filename} ({metadata.transfer_id}): "
                    f"tampered ciphertext or invalid key. Details: {dec_err}"
                ) from dec_err

            # 7. Verify SHA-256 and size
            actual_hash = hash_file(dec_path)
            if actual_hash.lower() != metadata.sha256_hash.lower():
                raise IntegrityError(
                    f"SHA-256 hash mismatch for {metadata.filename} ({metadata.transfer_id})! "
                    f"Expected: {metadata.sha256_hash}, Actual: {actual_hash}"
                )

            actual_size = dec_path.stat().st_size
            if actual_size != metadata.file_size:
                raise IntegrityError(
                    f"File size mismatch for {metadata.filename} ({metadata.transfer_id})! "
                    f"Expected: {metadata.file_size} bytes, Actual: {actual_size} bytes"
                )

            # In dry-run mode, stop here and report verification success without committing
            if is_dry_run:
                logger.info(
                    "[Dry Run] Verified transfer %s (%s)",
                    metadata.transfer_id,
                    metadata.filename,
                )
                return None, None

            # 8. Safe destination resolution with traversal prevention & deduplication
            final_path = resolve_destination(self.inbox_dir, metadata.filename)

            # 9. Atomic commit to final Inbox path
            self._commit_to_inbox(dec_path, final_path)
            if job:
                job.dest_path = str(final_path)
                if self.database:
                    self.database.update(job)

            # 10. Issue completion receipt with receiver_platform='macos'
            receipt = CompletionReceipt(
                transfer_id=metadata.transfer_id,
                final_path=str(final_path),
                file_size=metadata.file_size,
                sha256_hash=metadata.sha256_hash,
                receiver_platform=Platform.MACOS.value,
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
                job.drive_receipt_id = str(receipt_id) if receipt_id is not None else None
                if self.database:
                    self.database.update(job)

            # 12. Cleanup staged encrypted blob from Drive
            if self.cleanup_cloud_after_receipt:
                try:
                    self.cleanup_staged_blob(metadata.transfer_id)
                except Exception as e:
                    logger.warning(
                        "Could not delete cloud staged blob %s: %s", metadata.transfer_id, e
                    )

            send_notification(
                title="HomeVault — File Received",
                message=f"'{metadata.filename}' received from Windows workstation.",
            )

            return final_path, receipt

        except Exception as err:
            logger.error("Error processing incoming transfer %s: %s", metadata.transfer_id, err)
            if job:
                job.retry_count += 1
                retries = job.retry_count
                if retries >= job.max_retries:
                    if job.state != TransferState.FAILED:
                        try:
                            TransferStateMachine.transition(
                                job, TransferState.FAILED, error_message=str(err)
                            )
                        except Exception:
                            job.state = TransferState.FAILED
                            job.error_message = str(err)
                else:
                    if job.state == TransferState.VERIFYING:
                        try:
                            TransferStateMachine.transition(
                                job, TransferState.FAILED, error_message=str(err)
                            )
                            TransferStateMachine.transition(job, TransferState.QUEUED)
                        except Exception:
                            job.state = TransferState.QUEUED
                            job.error_message = str(err)
                    elif job.state == TransferState.DOWNLOADING:
                        try:
                            TransferStateMachine.transition(
                                job, TransferState.QUEUED, error_message=str(err)
                            )
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

    def poll_and_process(self) -> list[tuple[Path, CompletionReceipt]]:
        """Discover pending transfers and download/verify each one."""
        pending = self.discover_pending_transfers()
        if not pending:
            return []

        logger.info("Found %d pending inbound transfer(s) for macOS.", len(pending))
        delivered: list[tuple[Path, CompletionReceipt]] = []

        for meta in pending:
            try:
                dest, receipt = self.download_and_verify(meta)
                if dest and receipt:
                    delivered.append((dest, receipt))
                    logger.info("Delivered '%s' to %s", meta.filename, dest)
            except Exception as err:
                logger.error("Failed to process transfer %s: %s", meta.transfer_id, err)

        return delivered

    def _commit_to_inbox(self, src: Path, dest: Path) -> None:
        """Atomically commit the decrypted file to the final path and set safe permissions."""
        try:
            os.replace(src, dest)
        except OSError:
            shutil.move(str(src), str(dest))

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
                sender_platform=metadata.sender_platform or Platform.WINDOWS.value,
                target_platform=Platform.MACOS.value,
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
            receiver_platform=Platform.MACOS.value,
        )
        receipt_id = self.drive_client.upload_json_data(
            name=f"{job.transfer_id}.json",
            parent_id=folder_map.receipts_id,
            json_content=receipt.to_json(),
        )
        if job.state != TransferState.COMPLETED:
            try:
                TransferStateMachine.transition(job, TransferState.COMPLETED)
            except Exception:
                job.state = TransferState.COMPLETED
        if receipt_id is not None:
            job.drive_receipt_id = str(receipt_id)
        if self.database:
            self.database.update(job)
        if self.cleanup_cloud_after_receipt:
            try:
                self.cleanup_staged_blob(job.transfer_id)
            except Exception as e:
                logger.warning("Could not delete cloud staged blob %s: %s", job.transfer_id, e)

    def cleanup_staged_blob(self, transfer_id: str) -> bool:
        """Purge the staged encrypted blob from Google Drive after delivery."""
        folder_map = self.drive_client.ensure_folders()
        staged_filename = f"{transfer_id}.enc"
        remote_staged = self.drive_client.find_file(staged_filename, folder_map.staging_id)
        if remote_staged:
            return self.drive_client.delete_file(remote_staged["id"])
        return False
