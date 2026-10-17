"""Tests for macOS sender StagingManager."""

from unittest.mock import MagicMock, patch

import pytest

from homevault_protocol.crypto import generate_key
from homevault_protocol.drive.client import DriveClient, InsufficientStorageError
from homevault_protocol.drive.folders import DriveFolderMap
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt
from homevault_protocol.models import TransferJob, TransferMode, TransferState
from homevault_sender.staging import StagingManager


@pytest.fixture
def folder_map():
    return DriveFolderMap(
        root_id="root-1",
        staging_id="staging-2",
        metadata_id="metadata-3",
        receipts_id="receipts-4",
    )


class TestStagingManager:
    def test_stage_job_success(self, tmp_path, folder_map):
        source = tmp_path / "test_doc.pdf"
        source.write_bytes(b"Sensitive document content for staging")

        job = TransferJob(
            mode=TransferMode.COPY,
            source_path=str(source),
            filename="test_doc.pdf",
        )

        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = None  # Not previously staged
        drive_client.upload_file_resumable.return_value = "drive-staged-id-99"
        drive_client.upload_json_data.return_value = "drive-meta-id-100"

        manager = StagingManager(drive_client=drive_client, temp_dir=tmp_path / "temp")
        staged_job = manager.stage_job(job, encryption_key=key)

        assert staged_job.state == TransferState.QUEUED
        assert staged_job.drive_file_id == "drive-staged-id-99"
        assert staged_job.drive_metadata_id == "drive-meta-id-100"
        assert staged_job.sha256_hash == hash_file(source)

        drive_client.check_quota.assert_called_once_with(required_bytes=source.stat().st_size)
        drive_client.upload_file_resumable.assert_called_once()
        drive_client.upload_json_data.assert_called_once()

    def test_stage_job_idempotent_existing_staged(self, tmp_path, folder_map):
        source = tmp_path / "existing.txt"
        source.write_bytes(b"Data")

        job = TransferJob(source_path=str(source))
        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        # Staged file already exists on Drive
        drive_client.find_file.side_effect = [
            {"id": "already-uploaded-file-id"},  # find staged
            None,  # find metadata
        ]
        drive_client.upload_json_data.return_value = "new-meta-id"

        manager = StagingManager(drive_client=drive_client, temp_dir=tmp_path / "temp")
        staged_job = manager.stage_job(job, encryption_key=key)

        assert staged_job.drive_file_id == "already-uploaded-file-id"
        # upload_file_resumable should NOT have been called because it already existed!
        drive_client.upload_file_resumable.assert_not_called()

    def test_stage_job_aborts_on_insufficient_quota(self, tmp_path):
        source = tmp_path / "huge.bin"
        source.write_bytes(b"X" * 1024)

        job = TransferJob(source_path=str(source))
        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.check_quota.side_effect = InsufficientStorageError("Drive full")

        manager = StagingManager(drive_client=drive_client, temp_dir=tmp_path / "temp")
        with pytest.raises(InsufficientStorageError):
            manager.stage_job(job, encryption_key=key)

        assert job.state == TransferState.FAILED

    def test_finalize_move_success(self, tmp_path):
        source = tmp_path / "to_delete.txt"
        source.write_bytes(b"Original file to move")

        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="to_delete.txt",
            file_size=source.stat().st_size,
            sha256_hash=hash_file(source),
            drive_file_id="cloud-blob-id",
        )

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="D:\\HomeVault\\Inbox\\to_delete.txt",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client)

        result = manager.finalize_move(job, receipt, delete_cloud_staging=True)
        assert result is True
        assert not source.exists()  # Safely deleted!
        assert job.state == TransferState.COMPLETED
        drive_client.delete_file.assert_called_once_with("cloud-blob-id")

    def test_finalize_move_modified_source_fails_and_preserves_file(self, tmp_path):
        source = tmp_path / "modified.txt"
        source.write_bytes(b"Original content")
        recorded_hash = hash_file(source)

        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="modified.txt",
            file_size=source.stat().st_size,
            sha256_hash=recorded_hash,
            drive_file_id="cloud-blob-id",
        )

        # Modify source file after staging
        source.write_bytes(b"TAMPERED / CHANGED CONTENT!")

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="D:\\HomeVault\\Inbox\\modified.txt",
            file_size=len("Original content"),
            sha256_hash=recorded_hash,
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client)

        result = manager.finalize_move(job, receipt)
        assert result is False
        assert source.exists()  # Must NOT be deleted!
        assert job.state == TransferState.NEEDS_ATTENTION
        drive_client.delete_file.assert_not_called()
