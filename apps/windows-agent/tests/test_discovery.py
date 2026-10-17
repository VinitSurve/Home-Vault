"""Tests for Windows agent CloudDiscoveryManager: discovery, download, decryption, disk space, and recovery."""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.discovery import (
    CloudDiscoveryManager,
    InsufficientDiskSpaceError,
    IntegrityError,
)
from homevault_protocol.crypto import encrypt_file, generate_key
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.drive.folders import DriveFolderMap
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import TransferMetadata
from homevault_protocol.models import TransferJob, TransferMode, TransferState


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
    database = TransferDatabase(tmp_path / "test_receiver.db")
    database.migrate()
    yield database
    database.close()


class TestCloudDiscoveryManager:
    def test_discover_pending_transfers_filters_completed(self, tmp_path, folder_map):
        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map

        # Receipts on Drive: job-1.json is completed
        drive_client.list_files_in_folder.side_effect = [
            [{"name": "job-1.json"}],  # Receipts folder
            [{"id": "m1", "name": "job-1.json"}, {"id": "m2", "name": "job-2.json"}],  # Metadata folder
        ]

        # Metadata content for job-2 (job-1 should be skipped without even reading)
        drive_client.read_json_file.return_value = {
            "transfer_id": "job-2",
            "mode": "copy",
            "filename": "file2.txt",
            "file_size": 100,
            "sha256_hash": "hash2",
            "created_at": "2026-01-01T00:00:00Z",
        }

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=tmp_path / "inbox",
        )
        pending = manager.discover_pending_transfers()

        assert len(pending) == 1
        assert pending[0].transfer_id == "job-2"
        drive_client.read_json_file.assert_called_once_with("m2")

    def test_download_and_verify_success_with_db(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        # Prepare a plaintext file and encrypt it
        plaintext = tmp_path / "original.txt"
        plaintext.write_bytes(b"Top secret Windows deliverable")
        expected_hash = hash_file(plaintext)
        key = generate_key()

        encrypted = tmp_path / "encrypted.enc"
        encrypt_file(plaintext, encrypted, key)

        metadata = TransferMetadata(
            transfer_id="transfer-123",
            mode="copy",
            filename="original.txt",
            file_size=plaintext.stat().st_size,
            sha256_hash=expected_hash,
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.side_effect = [
            {"id": "staged-drive-id"},  # find staged file
            {"id": "staged-drive-id"},  # find staged for cleanup
        ]

        def mock_download(file_id, dest_path, **kwargs):
            dest_path.write_bytes(encrypted.read_bytes())

        drive_client.download_file_resumable.side_effect = mock_download
        drive_client.upload_json_data.return_value = "receipt-drive-id"

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            cleanup_cloud_after_receipt=True,
        )

        final_path, receipt = manager.download_and_verify(metadata, encryption_key=key)

        assert final_path.exists()
        assert final_path.read_bytes() == b"Top secret Windows deliverable"
        assert receipt.transfer_id == "transfer-123"
        assert receipt.sha256_hash == expected_hash

        # Verify SQLite persistent job record
        job = db.get("transfer-123")
        assert job is not None
        assert job.state == TransferState.COMPLETED
        assert job.dest_path == str(final_path)

        # Receipt uploaded to Drive and cloud blob deleted
        drive_client.upload_json_data.assert_called_once()
        drive_client.delete_file.assert_called_once_with("staged-drive-id")

    def test_duplicate_filename_preserves_existing_without_overwrite(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        # Existing file in inbox
        existing_file = inbox / "document.pdf"
        existing_file.write_bytes(b"Pre-existing local document")

        # New file with same name
        plaintext = tmp_path / "new.pdf"
        plaintext.write_bytes(b"Incoming new document from Mac")
        expected_hash = hash_file(plaintext)
        key = generate_key()

        encrypted = tmp_path / "new.enc"
        encrypt_file(plaintext, encrypted, key)

        metadata = TransferMetadata(
            transfer_id="transfer-dup-1",
            mode="copy",
            filename="document.pdf",
            file_size=plaintext.stat().st_size,
            sha256_hash=expected_hash,
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "staged-id"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: dest_path.write_bytes(encrypted.read_bytes())
        )
        drive_client.upload_json_data.return_value = "r-id"

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            cleanup_cloud_after_receipt=False,
        )

        final_path, receipt = manager.download_and_verify(metadata, encryption_key=key)

        # Existing file was NOT overwritten
        assert existing_file.read_bytes() == b"Pre-existing local document"
        # New file was placed with non-colliding name
        assert final_path == inbox / "document (1).pdf"
        assert final_path.read_bytes() == b"Incoming new document from Mac"

    def test_path_traversal_sanitized(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        plaintext = tmp_path / "evil.txt"
        plaintext.write_bytes(b"Attempted traversal payload")
        expected_hash = hash_file(plaintext)
        key = generate_key()

        encrypted = tmp_path / "evil.enc"
        encrypt_file(plaintext, encrypted, key)

        metadata = TransferMetadata(
            transfer_id="transfer-traversal",
            mode="copy",
            filename="../../Windows/System32/evil.dll",
            file_size=plaintext.stat().st_size,
            sha256_hash=expected_hash,
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "staged-id"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: dest_path.write_bytes(encrypted.read_bytes())
        )
        drive_client.upload_json_data.return_value = "r-id"

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            cleanup_cloud_after_receipt=False,
        )

        final_path, receipt = manager.download_and_verify(metadata, encryption_key=key)

        # Must be strictly within inbox
        assert final_path.parent == inbox
        assert final_path.name == "evil.dll"

    def test_insufficient_disk_space_aborts(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        metadata = TransferMetadata(
            transfer_id="transfer-huge",
            mode="copy",
            filename="huge.iso",
            file_size=100 * 1024 * 1024 * 1024,  # 100 GB
            sha256_hash="abc",
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
        )

        # Mock disk usage returning 1 GB free
        with patch("shutil.disk_usage", return_value=MagicMock(free=1024 * 1024 * 1024)):
            with pytest.raises(InsufficientDiskSpaceError):
                manager.download_and_verify(metadata, encryption_key=generate_key())

        drive_client.download_file_resumable.assert_not_called()

    def test_dry_run_mode_verifies_without_commit(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        plaintext = tmp_path / "test.txt"
        plaintext.write_bytes(b"Dry run validation")
        expected_hash = hash_file(plaintext)
        key = generate_key()

        encrypted = tmp_path / "test.enc"
        encrypt_file(plaintext, encrypted, key)

        metadata = TransferMetadata(
            transfer_id="transfer-dry",
            mode="copy",
            filename="test.txt",
            file_size=plaintext.stat().st_size,
            sha256_hash=expected_hash,
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "staged-id"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: dest_path.write_bytes(encrypted.read_bytes())
        )

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
        )

        final_path, receipt = manager.download_and_verify(metadata, encryption_key=key, dry_run=True)

        assert final_path is None
        assert receipt is None
        # No files written to inbox
        assert len(list(inbox.glob("*.txt"))) == 0
        # No receipt uploaded to Drive
        drive_client.upload_json_data.assert_not_called()

    def test_tampered_ciphertext_fails_integrity(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        metadata = TransferMetadata(
            transfer_id="transfer-tampered",
            mode="copy",
            filename="tampered.bin",
            file_size=16,
            sha256_hash="0000000000000000000000000000000000000000000000000000000000000000",
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "staged-id"}
        drive_client.download_file_resumable.side_effect = (
            lambda file_id, dest_path, **kw: dest_path.write_bytes(b"corrupt-data-not-gcm")
        )

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
        )

        with pytest.raises(IntegrityError):
            manager.download_and_verify(metadata, encryption_key=generate_key())

        # No user file in inbox
        user_files = [p for p in inbox.iterdir() if not p.name.startswith(".")]
        assert len(user_files) == 0
        drive_client.upload_json_data.assert_not_called()

        # Job in database flagged with failure
        job = db.get("transfer-tampered")
        assert job is not None
        assert job.retry_count == 1

    def test_restart_recovery_for_locally_completed_job(self, tmp_path, folder_map, db):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        # Existing delivered file in Inbox
        delivered = inbox / "already_done.pdf"
        delivered.write_bytes(b"Delivered content")

        # Job already marked COMPLETED in local SQLite
        job = TransferJob(
            transfer_id="transfer-rec-1",
            mode=TransferMode.COPY,
            state=TransferState.COMPLETED,
            filename="already_done.pdf",
            file_size=len("Delivered content"),
            sha256_hash=hash_file(delivered),
            dest_path=str(delivered),
        )
        db.insert(job)

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        # No receipts on Drive yet (e.g. power lost right before upload)
        drive_client.list_files_in_folder.side_effect = [
            [],  # Receipts folder
            [{"id": "m1", "name": "transfer-rec-1.json"}],  # Metadata folder
        ]
        drive_client.find_file.return_value = {"id": "staged-id"}

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            database=db,
            cleanup_cloud_after_receipt=True,
        )

        # Polling should detect local completion and recover receipt to Drive
        pending = manager.discover_pending_transfers()
        assert len(pending) == 0

        # Receipt uploaded to Drive and cloud blob purged
        drive_client.upload_json_data.assert_called_once()
        drive_client.delete_file.assert_called_once_with("staged-id")
