"""Tests for macOS receiver MacReceiver: discovery, routing, decryption, and integrity."""

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_protocol.crypto import encrypt_file, generate_key
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.drive.folders import DriveFolderMap
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt, TransferMetadata
from homevault_protocol.models import (
    Platform,
    TransferJob,
    TransferMode,
    TransferState,
)
from homevault_sender.receiver import (
    InsufficientDiskSpaceError,
    IntegrityError,
    MacReceiver,
)


@pytest.fixture
def folder_map():
    return DriveFolderMap(
        root_id="root-1",
        staging_id="staging-2",
        metadata_id="metadata-3",
        receipts_id="receipts-4",
    )


@pytest.fixture
def db(tmp_path):
    database = TransferDatabase(tmp_path / "test_mac_receiver.db")
    database.migrate()
    yield database
    database.close()


class TestMacReceiverRouting:
    def test_discover_pending_transfers_filters_by_platform_and_completed(
        self, tmp_path, folder_map
    ):
        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map

        # Receipts on Drive: job-completed.json is already finished
        drive_client.list_files_in_folder.side_effect = [
            [{"name": "job-completed.json"}],  # Receipts folder
            [
                {"id": "m1", "name": "job-completed.json"},
                {"id": "m2", "name": "job-mac.json"},
                {"id": "m3", "name": "job-win.json"},
                {"id": "m4", "name": "job-legacy.json"},
            ],  # Metadata folder
        ]

        def mock_read_json(file_id):
            if file_id == "m2":
                return {
                    "transfer_id": "job-mac",
                    "mode": "copy",
                    "filename": "file_mac.txt",
                    "file_size": 100,
                    "sha256_hash": "a" * 64,
                    "created_at": "2026-01-01T00:00:00Z",
                    "sender_platform": "windows",
                    "target_platform": "macos",
                }
            elif file_id == "m3":
                return {
                    "transfer_id": "job-win",
                    "mode": "copy",
                    "filename": "file_win.txt",
                    "file_size": 100,
                    "sha256_hash": "b" * 64,
                    "created_at": "2026-01-01T00:00:00Z",
                    "sender_platform": "macos",
                    "target_platform": "windows",
                }
            elif file_id == "m4":
                # Legacy metadata: no target_platform (Mac-to-Windows transfer)
                return {
                    "transfer_id": "job-legacy",
                    "mode": "copy",
                    "filename": "file_legacy.txt",
                    "file_size": 100,
                    "sha256_hash": "c" * 64,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            return {}

        drive_client.read_json_file.side_effect = mock_read_json

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=tmp_path / "MacInbox",
            temp_dir=tmp_path / "temp",
        )

        pending = receiver.discover_pending_transfers()

        # MUST discover only job-mac!
        # - job-completed: skipped (receipt exists)
        # - job-win: skipped (target_platform is windows)
        # - job-legacy: skipped (legacy metadata target defaults to windows)
        assert len(pending) == 1
        assert pending[0].transfer_id == "job-mac"
        assert pending[0].target_platform == "macos"


class TestMacReceiverExecution:
    def test_download_and_verify_success_with_db(self, tmp_path, folder_map, db):
        key = generate_key()
        inbox = tmp_path / "MacInbox"
        temp = tmp_path / "temp"

        # Prepare plaintext and encrypted file
        plain_file = tmp_path / "sample.docx"
        plain_file.write_bytes(b"Document from Windows workstation for macOS user")
        plain_hash = hash_file(plain_file)
        plain_size = plain_file.stat().st_size

        enc_file = tmp_path / "sample.enc"
        encrypt_file(plain_file, enc_file, key)

        metadata = TransferMetadata(
            transfer_id="transfer-win-to-mac-1",
            mode="copy",
            filename="sample.docx",
            file_size=plain_size,
            sha256_hash=plain_hash,
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "staged-drive-id"}

        def mock_download(file_id, dest_path, **kwargs):
            shutil.copy(enc_file, dest_path)

        drive_client.download_file_resumable.side_effect = mock_download
        drive_client.upload_json_data.return_value = "receipt-drive-id"

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=temp,
            encryption_key=key,
        )

        delivered_path, receipt = receiver.download_and_verify(metadata)

        assert delivered_path is not None
        assert delivered_path.exists()
        assert delivered_path.parent == inbox.resolve()
        assert delivered_path.name == "sample.docx"
        assert delivered_path.read_bytes() == b"Document from Windows workstation for macOS user"

        assert receipt is not None
        assert receipt.transfer_id == metadata.transfer_id
        assert receipt.receiver_platform == "macos"
        assert receipt.file_size == plain_size
        assert receipt.sha256_hash == plain_hash

        # Receipt uploaded to Drive with receiver_platform='macos'
        drive_client.upload_json_data.assert_called_once()
        _, upload_kwargs = drive_client.upload_json_data.call_args
        assert '"receiver_platform": "macos"' in upload_kwargs["json_content"]

        # Local SQLite state is COMPLETED with explicit platforms
        job = db.get("transfer-win-to-mac-1")
        assert job is not None
        assert job.state == TransferState.COMPLETED
        assert job.dest_path == str(delivered_path)
        assert job.sender_platform == Platform.WINDOWS
        assert job.target_platform == Platform.MACOS

    def test_duplicate_filename_preserves_existing_without_overwrite(
        self, tmp_path, folder_map, db
    ):
        key = generate_key()
        inbox = tmp_path / "MacInbox"
        inbox.mkdir(parents=True, exist_ok=True)

        # Existing file with same name in Inbox
        existing = inbox / "report.pdf"
        existing.write_bytes(b"EXISTING LOCAL MAC FILE")

        # New file from Windows
        incoming = tmp_path / "new_report.pdf"
        incoming.write_bytes(b"INCOMING WINDOWS REPORT")
        incoming_hash = hash_file(incoming)

        enc_file = tmp_path / "new_report.enc"
        encrypt_file(incoming, enc_file, key)

        metadata = TransferMetadata(
            transfer_id="transfer-dup-1",
            mode="copy",
            filename="report.pdf",
            file_size=incoming.stat().st_size,
            sha256_hash=incoming_hash,
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "blob-dup"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: shutil.copy(enc_file, dest_path)
        )
        drive_client.upload_json_data.return_value = "receipt-id"

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=tmp_path / "temp",
            encryption_key=key,
        )

        delivered_path, _ = receiver.download_and_verify(metadata)

        # Both files must exist!
        assert existing.read_bytes() == b"EXISTING LOCAL MAC FILE"
        assert delivered_path is not None
        assert delivered_path != existing
        assert delivered_path.name == "report (1).pdf"
        assert delivered_path.read_bytes() == b"INCOMING WINDOWS REPORT"

    def test_path_traversal_sanitized(self, tmp_path, folder_map, db):
        key = generate_key()
        inbox = tmp_path / "MacInbox"

        plain = tmp_path / "passwd"
        plain.write_bytes(b"root:x:0:0")
        enc = tmp_path / "passwd.enc"
        encrypt_file(plain, enc, key)

        metadata = TransferMetadata(
            transfer_id="transfer-traversal",
            mode="copy",
            filename="../../../../../etc/passwd",
            file_size=plain.stat().st_size,
            sha256_hash=hash_file(plain),
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "blob-trav"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: shutil.copy(enc, dest_path)
        )
        drive_client.upload_json_data.return_value = "receipt-trav-id"

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=tmp_path / "temp",
            encryption_key=key,
        )

        delivered_path, _ = receiver.download_and_verify(metadata)
        assert delivered_path is not None
        assert delivered_path.parent == inbox.resolve()
        assert delivered_path.name == "passwd"

    def test_insufficient_disk_space_aborts(self, tmp_path, folder_map):
        metadata = TransferMetadata(
            transfer_id="transfer-huge",
            mode="copy",
            filename="huge.bin",
            file_size=1000,
            sha256_hash="0" * 64,
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )
        drive_client = MagicMock(spec=DriveClient)
        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=tmp_path / "MacInbox",
            temp_dir=tmp_path / "temp",
            encryption_key="key",
        )

        with patch("shutil.disk_usage") as mock_usage:
            # Report only 100 bytes free
            mock_usage.return_value = shutil._ntuple_diskusage(1000000, 999900, 100)
            with pytest.raises(InsufficientDiskSpaceError):
                receiver.download_and_verify(metadata)

    def test_tampered_ciphertext_fails_integrity(self, tmp_path, folder_map, db):
        key = generate_key()
        inbox = tmp_path / "MacInbox"

        plain = tmp_path / "secret.txt"
        plain.write_bytes(b"Secret data")
        enc = tmp_path / "secret.enc"
        encrypt_file(plain, enc, key)

        # Tamper with the ciphertext
        corrupt_bytes = bytearray(enc.read_bytes())
        corrupt_bytes[20] ^= 0xFF
        enc.write_bytes(corrupt_bytes)

        metadata = TransferMetadata(
            transfer_id="transfer-corrupt",
            mode="copy",
            filename="secret.txt",
            file_size=plain.stat().st_size,
            sha256_hash=hash_file(plain),
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "blob-corrupt"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: shutil.copy(enc, dest_path)
        )

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=tmp_path / "temp",
            encryption_key=key,
        )

        with pytest.raises(IntegrityError):
            receiver.download_and_verify(metadata)

        # File must NEVER be committed to Inbox
        assert not (inbox / "secret.txt").exists()
        # Receipt must NOT be uploaded
        drive_client.upload_json_data.assert_not_called()

    def test_dry_run_mode_verifies_without_commit(self, tmp_path, folder_map, db):
        key = generate_key()
        inbox = tmp_path / "MacInbox"

        plain = tmp_path / "dry.txt"
        plain.write_bytes(b"Dry run payload")
        enc = tmp_path / "dry.enc"
        encrypt_file(plain, enc, key)

        metadata = TransferMetadata(
            transfer_id="transfer-dry",
            mode="copy",
            filename="dry.txt",
            file_size=plain.stat().st_size,
            sha256_hash=hash_file(plain),
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "blob-dry"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: shutil.copy(enc, dest_path)
        )

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=tmp_path / "temp",
            encryption_key=key,
            dry_run=True,
        )

        dest, receipt = receiver.download_and_verify(metadata)
        assert dest is None
        assert receipt is None
        assert not (inbox / "dry.txt").exists()
        drive_client.upload_json_data.assert_not_called()

    def test_restart_recovery_for_locally_completed_job(self, tmp_path, folder_map, db):
        inbox = tmp_path / "MacInbox"
        inbox.mkdir(parents=True, exist_ok=True)
        delivered_file = inbox / "recovered.txt"
        delivered_file.write_bytes(b"Already delivered")

        job = TransferJob(
            transfer_id="transfer-recovery",
            mode=TransferMode.COPY,
            state=TransferState.COMPLETED,
            filename="recovered.txt",
            file_size=delivered_file.stat().st_size,
            sha256_hash=hash_file(delivered_file),
            dest_path=str(delivered_file),
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        db.insert(job)

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.list_files_in_folder.side_effect = [
            [],  # Receipts folder empty
            [{"id": "meta-rec", "name": "transfer-recovery.json"}],  # Metadata
        ]

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=tmp_path / "temp",
        )

        pending = receiver.discover_pending_transfers()
        # Should be empty because it recovered the receipt instead of re-downloading
        assert len(pending) == 0

        # Receipt re-uploaded with receiver_platform='macos'
        drive_client.upload_json_data.assert_called_once()
        _, kwargs = drive_client.upload_json_data.call_args
        assert kwargs["name"] == "transfer-recovery.json"
    def test_recovery_when_delivery_succeeded_but_receipt_upload_failed(
        self, tmp_path, folder_map, db
    ):
        inbox = tmp_path / "MacInbox"
        inbox.mkdir(parents=True, exist_ok=True)
        delivered_file = inbox / "recovered_partial.txt"
        delivered_file.write_bytes(b"Delivered content before network failure")

        meta = TransferMetadata(
            transfer_id="transfer-partially-failed",
            mode="copy",
            filename="recovered_partial.txt",
            file_size=delivered_file.stat().st_size,
            sha256_hash=hash_file(delivered_file),
            created_at="2026-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )

        job = TransferJob(
            transfer_id="transfer-partially-failed",
            mode=TransferMode.COPY,
            state=TransferState.VERIFYING,
            filename="recovered_partial.txt",
            file_size=delivered_file.stat().st_size,
            sha256_hash=hash_file(delivered_file),
            dest_path=str(delivered_file),
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        db.insert(job)

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.upload_json_data.return_value = "receipt-id-999"

        receiver = MacReceiver(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            temp_dir=tmp_path / "temp",
            encryption_key="test-key",
        )

        final_path, receipt = receiver.download_and_verify(meta)

        assert final_path == delivered_file
        assert receipt is not None
        assert receipt.receiver_platform == "macos"
        assert receipt.transfer_id == "transfer-partially-failed"
        drive_client.download_file_resumable.assert_not_called()
        drive_client.upload_json_data.assert_called_once()

        updated_job = db.get("transfer-partially-failed")
        assert updated_job.state == TransferState.COMPLETED
        assert updated_job.drive_receipt_id == "receipt-id-999"

    def test_key_retrieval(self, tmp_path):
        receiver = MacReceiver(
            drive_client=MagicMock(),
            inbox_dir=tmp_path,
            encryption_key="explicit-key",
        )
        assert receiver.get_encryption_key() == "explicit-key"

        receiver_env = MacReceiver(drive_client=MagicMock(), inbox_dir=tmp_path)
        with patch.dict(os.environ, {"HOMEVAULT_ENCRYPTION_KEY": "env-key"}):
            assert receiver_env.get_encryption_key() == "env-key"

        receiver_keyring = MacReceiver(drive_client=MagicMock(), inbox_dir=tmp_path)
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value="keyring-key"):
                assert receiver_keyring.get_encryption_key() == "keyring-key"

        receiver_missing = MacReceiver(drive_client=MagicMock(), inbox_dir=tmp_path)
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value=None):
                with pytest.raises(RuntimeError, match="HomeVault encryption key not found in macOS Keychain"):
                    receiver_missing.get_encryption_key()
