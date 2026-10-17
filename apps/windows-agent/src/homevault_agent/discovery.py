"""Windows receiver Google Drive discovery, download, and receipt issuance.

Coordinates:
- Independent discovery of pending transfers by querying HomeVault/Metadata/.
- Deduplication against existing receipts in HomeVault/Receipts/.
- Resumable download of encrypted staged payloads.
- Authenticated decryption (AES-256-GCM) and streaming SHA-256 integrity verification.
- Path traversal prevention, safe filename resolution, and non-colliding commit to Inbox.
- Generation and upload of completion receipts to HomeVault/Receipts/.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional

from homevault_protocol.crypto import decrypt_file, decrypt_file_streaming
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.filenames import resolve_destination
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt, TransferMetadata

logger = logging.getLogger(__name__)

STREAMING_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB threshold for chunked decryption


class IntegrityError(Exception):
    """Raised when downloaded file fails SHA-256 checksum or authenticated decryption."""


class CloudDiscoveryManager:
    """Manages Drive polling, download, decryption, and receipting on Windows."""

    def __init__(
        self,
        drive_client: DriveClient,
        inbox_dir: Path,
        database: Optional[TransferDatabase] = None,
        temp_dir: Optional[Path] = None,
    ) -> None:
        self.drive_client = drive_client
        self.inbox_dir = Path(inbox_dir)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir()) / "homevault_agent"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def discover_pending_transfers(self) -> list[TransferMetadata]:
        """Discover all pending transfers from Drive metadata not yet acknowledged.

        Returns:
            List of TransferMetadata objects awaiting download.
        """
        folder_map = self.drive_client.ensure_folders()

        # 1. Fetch existing receipts to filter out completed transfers
        receipt_files = self.drive_client.list_files_in_folder(folder_map.receipts_id)
        completed_ids = {
            f["name"][:-5]
            for f in receipt_files
            if f["name"].endswith(".json")
        }

        # 2. Fetch metadata records
        metadata_files = self.drive_client.list_files_in_folder(folder_map.metadata_id)
        pending: list[TransferMetadata] = []

        for meta_file in metadata_files:
            if not meta_file["name"].endswith(".json"):
                continue

            transfer_id = meta_file["name"][:-5]
            if transfer_id in completed_ids:
                continue  # Already finished and receipted

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
    ) -> tuple[Path, CompletionReceipt]:
        """Download, decrypt, verify, safely store a transfer, and issue a receipt.

        Args:
            metadata: TransferMetadata for the pending job.
            encryption_key: Base64-encoded AES-256 decryption key.
            progress_callback: Optional callback receiving (bytes_downloaded, total_bytes).

        Returns:
            Tuple of (final_destination_path, CompletionReceipt).

        Raises:
            FileNotFoundError: If the staged payload is absent on Drive.
            IntegrityError: If decryption fails or SHA-256 mismatch is detected.
        """
        folder_map = self.drive_client.ensure_folders()
        staged_filename = f"{metadata.transfer_id}.enc"
        remote_staged = self.drive_client.find_file(staged_filename, folder_map.staging_id)

        if not remote_staged:
            raise FileNotFoundError(
                f"Staged file {staged_filename} not found on Google Drive in Staging/."
            )

        enc_path = self.temp_dir / f"{metadata.transfer_id}.enc"
        dec_path = self.temp_dir / f"{metadata.transfer_id}.dec"

        try:
            # 1. Download encrypted payload in chunks
            self.drive_client.download_file_resumable(
                file_id=remote_staged["id"],
                dest_path=enc_path,
                progress_callback=progress_callback,
            )

            # 2. Authenticated decryption
            try:
                if metadata.file_size > STREAMING_THRESHOLD_BYTES:
                    decrypt_file_streaming(enc_path, dec_path, encryption_key)
                else:
                    decrypt_file(enc_path, dec_path, encryption_key)
            except Exception as err:
                raise IntegrityError(
                    f"Decryption failed for transfer {metadata.transfer_id}: invalid key or corrupted ciphertext. ({err})"
                ) from err

            # 3. SHA-256 integrity verification
            actual_hash = hash_file(dec_path)
            if actual_hash.lower() != metadata.sha256_hash.lower():
                raise IntegrityError(
                    f"SHA-256 integrity mismatch for {metadata.transfer_id}: "
                    f"expected {metadata.sha256_hash}, got {actual_hash}."
                )

            # 4. Safe destination resolution with traversal prevention & deduplication
            final_path = resolve_destination(self.inbox_dir, metadata.filename)

            # 5. Atomic commit to final Inbox path
            dec_path.replace(final_path)

            # 6. Issue completion receipt and upload to Drive
            receipt = CompletionReceipt(
                transfer_id=metadata.transfer_id,
                final_path=str(final_path),
                file_size=metadata.file_size,
                sha256_hash=metadata.sha256_hash,
                receiver_platform="windows",
            )
            receipt_filename = f"{metadata.transfer_id}.json"
            self.drive_client.upload_json_data(
                name=receipt_filename,
                parent_id=folder_map.receipts_id,
                json_content=receipt.to_json(),
            )

            return final_path, receipt

        finally:
            enc_path.unlink(missing_ok=True)
            dec_path.unlink(missing_ok=True)

    def cleanup_staged_blob(self, transfer_id: str) -> bool:
        """Purge the staged encrypted blob from Google Drive after delivery."""
        folder_map = self.drive_client.ensure_folders()
        staged_filename = f"{transfer_id}.enc"
        remote_staged = self.drive_client.find_file(staged_filename, folder_map.staging_id)
        if remote_staged:
            return self.drive_client.delete_file(remote_staged["id"])
        return False
