"""Tests for Windows agent CloudDiscoveryManager."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.discovery import CloudDiscoveryManager, IntegrityError
from homevault_protocol.crypto import encrypt_file, generate_key
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.drive.folders import DriveFolderMap
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import TransferMetadata


@pytest.fixture
def folder_map():
    return DriveFolderMap(
        root_id="root-1",
        staging_id="staging-2",
        metadata_id="metadata-3",
        receipts_id="receipts-4",
    )


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

    def test_download_and_verify_success(self, tmp_path, folder_map):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        temp_dir = tmp_path / "temp"

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
        drive_client.find_file.return_value = {"id": "staged-drive-id"}

        # When download_file_resumable is called, copy our encrypted file to dest_path
        def mock_download(file_id, dest_path, **kwargs):
            dest_path.write_bytes(encrypted.read_bytes())

        drive_client.download_file_resumable.side_effect = mock_download
        drive_client.upload_json_data.return_value = "receipt-drive-id"

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            temp_dir=temp_dir,
        )

        final_path, receipt = manager.download_and_verify(metadata, encryption_key=key)

        assert final_path.exists()
        assert final_path.read_bytes() == b"Top secret Windows deliverable"
        assert receipt.transfer_id == "transfer-123"
        assert receipt.sha256_hash == expected_hash

        # Receipt should have been uploaded to Receipts folder
        drive_client.upload_json_data.assert_called_once()
        args, kwargs = drive_client.upload_json_data.call_args
        assert kwargs["parent_id"] == folder_map.receipts_id
        assert kwargs["name"] == "transfer-123.json"

    def test_download_and_verify_tampered_fails(self, tmp_path, folder_map):
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        key = generate_key()
        metadata = TransferMetadata(
            transfer_id="transfer-corrupt",
            mode="copy",
            filename="corrupt.txt",
            file_size=10,
            sha256_hash="0000000000000000000000000000000000000000000000000000000000000000",
            created_at="2026-01-01T00:00:00Z",
        )

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = {"id": "staged-id"}

        # Return garbage bytes
        def mock_download(file_id, dest_path, **kwargs):
            dest_path.write_bytes(b"garbage-bytes-not-encrypted")

        drive_client.download_file_resumable.side_effect = mock_download

        manager = CloudDiscoveryManager(
            drive_client=drive_client,
            inbox_dir=inbox,
            temp_dir=tmp_path / "temp",
        )

        with pytest.raises(IntegrityError):
            manager.download_and_verify(metadata, encryption_key=key)

        # Nothing should be committed to inbox
        assert len(list(inbox.iterdir())) == 0
        # No receipt uploaded
        drive_client.upload_json_data.assert_not_called()
