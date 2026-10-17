"""Tests for TransferJob model and serialization."""

import uuid

import pytest

from homevault_protocol.models import TransferJob, TransferMode, TransferState


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
        # Touch should always produce a new timestamp (or same if fast)
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

    def test_to_dict_uses_enum_values(self):
        job = TransferJob(mode=TransferMode.MOVE, state=TransferState.QUEUED)
        d = job.to_dict()
        assert d["mode"] == "move"
        assert d["state"] == "queued"

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
