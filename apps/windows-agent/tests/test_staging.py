"""Tests for Windows sender StagingManager."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.staging import STREAMING_THRESHOLD_BYTES, StagingManager
from homevault_protocol.crypto import generate_key
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient, InsufficientStorageError
from homevault_protocol.drive.folders import DriveFolderMap
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt
from homevault_protocol.models import (
    Platform,
    TransferJob,
    TransferMode,
    TransferState,
)


@pytest.fixture
def folder_map() -> DriveFolderMap:
    return DriveFolderMap(
        root_id="root-1",
        staging_id="staging-2",
        metadata_id="metadata-3",
        receipts_id="receipts-4",
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> TransferDatabase:
    db = TransferDatabase(tmp_path / "staging_test.db")
    db.migrate()
    return db


class TestWindowsStagingManager:
    def test_stage_job_success(
        self, tmp_path: Path, folder_map: DriveFolderMap, temp_db: TransferDatabase
    ) -> None:
        source = tmp_path / "windows_doc.pdf"
        source.write_bytes(b"Confidential payload from Windows workstation")

        job = TransferJob(
            mode=TransferMode.COPY,
            source_path=str(source),
            filename="windows_doc.pdf",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = None  # Not previously staged
        drive_client.upload_file_resumable.return_value = "drive-staged-id-win"
        drive_client.upload_json_data.return_value = "drive-meta-id-win"

        manager = StagingManager(
            drive_client=drive_client, database=temp_db, temp_dir=tmp_path / "temp"
        )
        staged_job = manager.stage_job(job, encryption_key=key)

        assert staged_job.state == TransferState.QUEUED
        assert staged_job.drive_file_id == "drive-staged-id-win"
        assert staged_job.drive_metadata_id == "drive-meta-id-win"
        assert staged_job.sha256_hash == hash_file(source)
        assert staged_job.sender_platform == Platform.WINDOWS
        assert staged_job.target_platform == Platform.MACOS

        drive_client.check_quota.assert_called_once_with(required_bytes=source.stat().st_size)
        drive_client.upload_file_resumable.assert_called_once()
        drive_client.upload_json_data.assert_called_once()

        # Verify metadata uploaded has explicit Windows sender and macOS target
        _, kwargs = drive_client.upload_json_data.call_args
        meta_dict = json.loads(kwargs["json_content"])
        assert meta_dict["sender_platform"] == "windows"
        assert meta_dict["target_platform"] == "macos"
        assert meta_dict["transfer_id"] == job.transfer_id

    def test_stage_job_idempotent_existing_staged(
        self, tmp_path: Path, folder_map: DriveFolderMap
    ) -> None:
        source = tmp_path / "existing_win.txt"
        source.write_bytes(b"Data from Windows")

        job = TransferJob(
            source_path=str(source),
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.side_effect = [
            {"id": "already-staged-id"},
            None,
        ]
        drive_client.upload_json_data.return_value = "new-meta-id"

        manager = StagingManager(drive_client=drive_client, temp_dir=tmp_path / "temp")
        staged_job = manager.stage_job(job, encryption_key=key)

        assert staged_job.drive_file_id == "already-staged-id"
        drive_client.upload_file_resumable.assert_not_called()

    def test_stage_job_aborts_on_insufficient_quota(self, tmp_path: Path) -> None:
        source = tmp_path / "large.bin"
        source.write_bytes(b"X" * 1024)

        job = TransferJob(
            source_path=str(source),
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.check_quota.side_effect = InsufficientStorageError("Drive full")

        manager = StagingManager(drive_client=drive_client, temp_dir=tmp_path / "temp")
        with pytest.raises(InsufficientStorageError):
            manager.stage_job(job, encryption_key=key)

        assert job.state == TransferState.FAILED

    def test_stage_job_streaming_threshold(
        self, tmp_path: Path, folder_map: DriveFolderMap
    ) -> None:
        source = tmp_path / "mock_stream.bin"
        source.write_bytes(b"data")

        job = TransferJob(
            source_path=str(source),
            file_size=STREAMING_THRESHOLD_BYTES + 10,
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        key = generate_key()

        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map
        drive_client.find_file.return_value = None
        drive_client.upload_file_resumable.return_value = "stream-id"
        drive_client.upload_json_data.return_value = "meta-id"

        manager = StagingManager(drive_client=drive_client, temp_dir=tmp_path / "temp")

        with patch("homevault_agent.staging.encrypt_file_streaming") as mock_streaming:
            manager.stage_job(job, encryption_key=key)
            mock_streaming.assert_called_once()

    def test_check_completion(self, folder_map: DriveFolderMap) -> None:
        drive_client = MagicMock(spec=DriveClient)
        drive_client.ensure_folders.return_value = folder_map

        manager = StagingManager(drive_client=drive_client)
        job = TransferJob(
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )

        # 1. No receipt on Drive
        drive_client.find_file.return_value = None
        assert manager.check_completion(job) is None

        # 2. Receipt present
        drive_client.find_file.return_value = {"id": "receipt-drive-id"}
        drive_client.read_json_file.return_value = {
            "transfer_id": job.transfer_id,
            "final_path": "/Users/user/HomeVault/test.txt",
            "file_size": 100,
            "sha256_hash": "a" * 64,
            "receiver_platform": "macos",
        }

        receipt = manager.check_completion(job)
        assert receipt is not None
        assert receipt.transfer_id == job.transfer_id
        assert receipt.receiver_platform == "macos"

    def test_finalize_move_success(self, tmp_path: Path, temp_db: TransferDatabase) -> None:
        source = tmp_path / "win_to_move.txt"
        source.write_bytes(b"Original Windows content to move to Mac")

        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="win_to_move.txt",
            file_size=source.stat().st_size,
            sha256_hash=hash_file(source),
            drive_file_id="cloud-blob-id-99",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/win_to_move.txt",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client, database=temp_db)

        result = manager.finalize_move(job, receipt, delete_cloud_staging=True)
        assert result is True
        assert not source.exists()  # Safely deleted!
        assert job.state == TransferState.COMPLETED
        assert job.dest_path == "/Users/user/HomeVault Received/win_to_move.txt"
        drive_client.delete_file.assert_called_once_with("cloud-blob-id-99")

        # Database updated
        saved = temp_db.get(job.transfer_id)
        assert saved is not None
        assert saved.state == TransferState.COMPLETED

    def test_finalize_move_platform_mismatch_preserves_file(
        self, tmp_path: Path, temp_db: TransferDatabase
    ) -> None:
        source = tmp_path / "protected_file.txt"
        source.write_bytes(b"Must not be deleted on invalid platform receipt")

        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="protected_file.txt",
            file_size=source.stat().st_size,
            sha256_hash=hash_file(source),
            drive_file_id="cloud-blob-id",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        # Receipt incorrectly has receiver_platform="windows" instead of "macos"
        invalid_receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="C:\\Users\\Admin\\Inbox\\protected_file.txt",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            receiver_platform="windows",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client, database=temp_db)

        result = manager.finalize_move(job, invalid_receipt)
        assert result is False
        assert source.exists()  # File must NOT be deleted!
        assert job.state == TransferState.NEEDS_ATTENTION
        drive_client.delete_file.assert_not_called()

    def test_finalize_move_modified_source_fails_and_preserves_file(
        self, tmp_path: Path, temp_db: TransferDatabase
    ) -> None:
        source = tmp_path / "modified_win.txt"
        source.write_bytes(b"Original content")
        recorded_hash = hash_file(source)

        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="modified_win.txt",
            file_size=source.stat().st_size,
            sha256_hash=recorded_hash,
            drive_file_id="cloud-blob-id",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        # Modify source file after staging
        source.write_bytes(b"TAMPERED FILE CONTENT!")

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/modified_win.txt",
            file_size=len(b"Original content"),
            sha256_hash=recorded_hash,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client, database=temp_db)

        result = manager.finalize_move(job, receipt)
        assert result is False
        assert source.exists()  # MUST NOT BE DELETED!
        assert job.state == TransferState.NEEDS_ATTENTION
        drive_client.delete_file.assert_not_called()

    def test_finalize_move_missing_source_fails(
        self, tmp_path: Path, temp_db: TransferDatabase
    ) -> None:
        missing_source = tmp_path / "already_gone.txt"

        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.QUEUED,
            source_path=str(missing_source),
            filename="already_gone.txt",
            file_size=10,
            sha256_hash="e" * 64,
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/already_gone.txt",
            file_size=10,
            sha256_hash="e" * 64,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client, database=temp_db)

        result = manager.finalize_move(job, receipt)
        assert result is False
        assert job.state == TransferState.NEEDS_ATTENTION

    def test_finalize_move_non_move_job_raises(self) -> None:
        job = TransferJob(
            mode=TransferMode.COPY,
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/path",
            file_size=0,
            sha256_hash="",
            receiver_platform="macos",
        )
        manager = StagingManager(drive_client=MagicMock())
        with pytest.raises(ValueError, match="not a Move job"):
            manager.finalize_move(job, receipt)

    def test_finalize_copy_success(self, tmp_path: Path, temp_db: TransferDatabase) -> None:
        source = tmp_path / "keep_me.txt"
        source.write_bytes(b"Retain this source file")

        job = TransferJob(
            mode=TransferMode.COPY,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="keep_me.txt",
            file_size=source.stat().st_size,
            sha256_hash=hash_file(source),
            drive_file_id="blob-copy-id",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/keep_me.txt",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client, database=temp_db)

        result = manager.finalize_copy(job, receipt, delete_cloud_staging=True)
        assert result is True
        assert source.exists()  # Kept!
        assert job.state == TransferState.COMPLETED
        assert job.dest_path == "/Users/user/HomeVault Received/keep_me.txt"
        drive_client.delete_file.assert_called_once_with("blob-copy-id")

    def test_finalize_copy_mismatched_receipt_fails(
        self, tmp_path: Path, temp_db: TransferDatabase
    ) -> None:
        source = tmp_path / "copy_err.txt"
        source.write_bytes(b"content")

        job = TransferJob(
            mode=TransferMode.COPY,
            state=TransferState.QUEUED,
            source_path=str(source),
            filename="copy_err.txt",
            file_size=source.stat().st_size,
            sha256_hash=hash_file(source),
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        temp_db.insert(job)

        # Receipt hash mismatch
        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/copy_err.txt",
            file_size=job.file_size,
            sha256_hash="0" * 64,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        manager = StagingManager(drive_client=drive_client, database=temp_db)

        result = manager.finalize_copy(job, receipt)
        assert result is False
        assert job.state == TransferState.NEEDS_ATTENTION
