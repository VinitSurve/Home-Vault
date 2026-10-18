"""Tests for TransferJob model and serialization."""

import uuid

import pytest

from homevault_protocol.models import (
    Platform,
    TransferJob,
    TransferMode,
    TransferState,
    validate_platform,
)


class TestPlatform:
    def test_values(self):
        assert Platform.MACOS.value == "macos"
        assert Platform.WINDOWS.value == "windows"

    def test_from_string_case_insensitive(self):
        assert validate_platform("macos") == Platform.MACOS
        assert validate_platform("MACOS") == Platform.MACOS
        assert validate_platform("  Windows  ") == Platform.WINDOWS
        assert validate_platform(Platform.MACOS) == Platform.MACOS

    def test_invalid_platform_raises(self):
        with pytest.raises(ValueError, match="Supported platforms"):
            validate_platform("linux")
        with pytest.raises(ValueError, match="Supported platforms"):
            validate_platform("android")

    def test_empty_or_non_string_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            validate_platform("")
        with pytest.raises(ValueError, match="non-empty string"):
            validate_platform("   ")
        with pytest.raises(ValueError, match="non-empty string"):
            validate_platform(None)


class TestTransferMode:
    def test_copy_value(self):
        assert TransferMode.COPY.value == "copy"

    def test_move_value(self):
        assert TransferMode.MOVE.value == "move"

    def test_from_string(self):
        assert TransferMode("copy") == TransferMode.COPY
        assert TransferMode("move") == TransferMode.MOVE

    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            TransferMode("delete")


class TestTransferState:
    def test_all_states_defined(self):
        expected = {
            "pending_upload", "uploading", "queued",
            "downloading", "verifying", "completed",
            "failed", "cancelled", "needs_attention",
        }
        actual = {s.value for s in TransferState}
        assert actual == expected

    def test_from_string(self):
        assert TransferState("queued") == TransferState.QUEUED


class TestTransferJob:
    def test_default_values(self):
        job = TransferJob()
        assert job.mode == TransferMode.COPY
        assert job.state == TransferState.PENDING_UPLOAD
        assert job.retry_count == 0
        assert job.max_retries == 5
        assert job.source_path is None
        assert job.drive_file_id is None
        assert job.sender_platform == Platform.MACOS
        assert job.target_platform == Platform.WINDOWS

    def test_windows_sender_defaults_to_macos_target(self):
        job = TransferJob(sender_platform=Platform.WINDOWS)
        assert job.sender_platform == Platform.WINDOWS
        assert job.target_platform == Platform.MACOS

    def test_explicit_valid_platforms(self):
        job = TransferJob(
            sender_platform="windows",
            target_platform="macos",
        )
        assert job.sender_platform == Platform.WINDOWS
        assert job.target_platform == Platform.MACOS

    def test_same_sender_and_target_platform_raises(self):
        with pytest.raises(ValueError, match="cannot be the same"):
            TransferJob(
                sender_platform=Platform.MACOS,
                target_platform=Platform.MACOS,
            )
        with pytest.raises(ValueError, match="cannot be the same"):
            TransferJob(
                sender_platform="windows",
                target_platform="windows",
            )

    def test_invalid_platform_in_job_raises(self):
        with pytest.raises(ValueError, match="Supported platforms"):
            TransferJob(sender_platform="linux")
        with pytest.raises(ValueError, match="non-empty string"):
            TransferJob(sender_platform=None)
        with pytest.raises(ValueError, match="Supported platforms"):
            TransferJob(target_platform="android")

    def test_from_dict_with_invalid_platform_raises(self):
        data = {
            "transfer_id": "test-id",
            "mode": "copy",
            "state": "pending_upload",
            "filename": "test.txt",
            "sender_platform": "invalid_os",
        }
        with pytest.raises(ValueError, match="Supported platforms"):
            TransferJob.from_dict(data)

    def test_transfer_id_is_uuid(self):
        job = TransferJob()
        parsed = uuid.UUID(job.transfer_id)
        assert parsed.version == 4

    def test_unique_ids(self):
        jobs = [TransferJob() for _ in range(100)]
        ids = {j.transfer_id for j in jobs}
        assert len(ids) == 100

    def test_touch_updates_timestamp(self):
        job = TransferJob()
        original = job.updated_at
        job.touch()
        assert job.updated_at >= original

    def test_round_trip_serialization(self):
        job = TransferJob(
            mode=TransferMode.MOVE,
            state=TransferState.UPLOADING,
            source_path="/Users/test/file.pdf",
            filename="file.pdf",
            file_size=123456,
            sha256_hash="abc123",
            drive_file_id="drive-id-1",
            error_message="test error",
            sender_platform=Platform.MACOS,
            target_platform=Platform.WINDOWS,
        )
        d = job.to_dict()
        restored = TransferJob.from_dict(d)

        assert restored.transfer_id == job.transfer_id
        assert restored.mode == TransferMode.MOVE
        assert restored.state == TransferState.UPLOADING
        assert restored.source_path == "/Users/test/file.pdf"
        assert restored.filename == "file.pdf"
        assert restored.file_size == 123456
        assert restored.sha256_hash == "abc123"
        assert restored.drive_file_id == "drive-id-1"
        assert restored.error_message == "test error"
        assert restored.sender_platform == Platform.MACOS
        assert restored.target_platform == Platform.WINDOWS

    def test_to_dict_uses_enum_values(self):
        job = TransferJob(mode=TransferMode.MOVE, state=TransferState.QUEUED)
        d = job.to_dict()
        assert d["mode"] == "move"
        assert d["state"] == "queued"
        assert d["sender_platform"] == "macos"
        assert d["target_platform"] == "windows"

    def test_from_dict_with_minimal_data(self):
        data = {
            "transfer_id": "test-id",
            "mode": "copy",
            "state": "pending_upload",
            "filename": "test.txt",
        }
        job = TransferJob.from_dict(data)
        assert job.transfer_id == "test-id"
        assert job.file_size == 0
        assert job.retry_count == 0
        assert job.sender_platform == Platform.MACOS
        assert job.target_platform == Platform.WINDOWS

    def test_from_dict_legacy_row_defaults_platforms(self):
        # Simulates a row retrieved from a v1 database without platform columns
        legacy_data = {
            "transfer_id": "legacy-id",
            "mode": "move",
            "state": "completed",
            "filename": "old.zip",
            "file_size": 500,
            "sha256_hash": "deadbeef",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:01:00Z",
        }
        job = TransferJob.from_dict(legacy_data)
        assert job.sender_platform == Platform.MACOS
        assert job.target_platform == Platform.WINDOWS

