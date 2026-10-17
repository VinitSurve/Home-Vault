"""Tests for macOS SenderQueue: persistence, file validation, cancellation, and retries."""

from pathlib import Path

import pytest

from homevault_protocol.database import TransferDatabase
from homevault_protocol.hashing import hash_file
from homevault_protocol.models import TransferMode, TransferState
from homevault_sender.queue import SenderQueue


@pytest.fixture
def db(tmp_path):
    database = TransferDatabase(tmp_path / "sender_queue_test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def queue(db):
    return SenderQueue(database=db)


class TestSenderQueue:
    def test_enqueue_file_copy(self, queue, tmp_path):
        sample = tmp_path / "sample.pdf"
        sample.write_bytes(b"Sample PDF content for transfer")

        job = queue.enqueue_file(sample, mode=TransferMode.COPY)

        assert job.filename == "sample.pdf"
        assert job.mode == TransferMode.COPY
        assert job.state == TransferState.PENDING_UPLOAD
        assert job.file_size == sample.stat().st_size
        assert job.sha256_hash == hash_file(sample)
        assert job.source_path == str(sample.resolve())

        # Verify persisted in database
        persisted = queue.get_job(job.transfer_id)
        assert persisted is not None
        assert persisted.transfer_id == job.transfer_id
        assert persisted.filename == "sample.pdf"

    def test_enqueue_file_move(self, queue, tmp_path):
        sample = tmp_path / "to_move.bin"
        sample.write_bytes(b"Data to move")

        job = queue.enqueue_file(sample, mode=TransferMode.MOVE)
        assert job.mode == TransferMode.MOVE
        assert job.state == TransferState.PENDING_UPLOAD

    def test_enqueue_missing_file_raises(self, queue, tmp_path):
        missing = tmp_path / "nonexistent.file"
        with pytest.raises(FileNotFoundError):
            queue.enqueue_file(missing)

    def test_enqueue_directory_raises(self, queue, tmp_path):
        dir_path = tmp_path / "a_directory"
        dir_path.mkdir()
        with pytest.raises(ValueError, match="directories are not supported"):
            queue.enqueue_file(dir_path)

    def test_queue_persistence_across_instances(self, tmp_path):
        db_path = tmp_path / "persistent_sender.db"
        db1 = TransferDatabase(db_path)
        q1 = SenderQueue(database=db1)

        sample = tmp_path / "persist.txt"
        sample.write_bytes(b"Persistence test")
        job = q1.enqueue_file(sample)
        transfer_id = job.transfer_id
        db1.close()

        # Re-open with new database and queue instances
        db2 = TransferDatabase(db_path)
        q2 = SenderQueue(database=db2)
        recovered = q2.get_job(transfer_id)

        assert recovered is not None
        assert recovered.filename == "persist.txt"
        assert recovered.state == TransferState.PENDING_UPLOAD
        db2.close()

    def test_get_pending_uploads(self, queue, tmp_path):
        f1 = tmp_path / "f1.txt"
        f1.write_text("1")
        f2 = tmp_path / "f2.txt"
        f2.write_text("2")

        j1 = queue.enqueue_file(f1)
        j2 = queue.enqueue_file(f2)

        pending = queue.get_pending_uploads()
        assert len(pending) == 2
        ids = {p.transfer_id for p in pending}
        assert j1.transfer_id in ids
        assert j2.transfer_id in ids

    def test_cancel_pending_job(self, queue, tmp_path):
        f = tmp_path / "cancel.txt"
        f.write_text("cancel me")
        job = queue.enqueue_file(f)

        assert queue.cancel_job(job.transfer_id) is True
        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.CANCELLED

        # Cannot cancel again
        assert queue.cancel_job(job.transfer_id) is False

    def test_retry_failed_job(self, queue, tmp_path, db):
        f = tmp_path / "retry.txt"
        f.write_text("retry me")
        job = queue.enqueue_file(f)

        # Force state to FAILED
        job.state = TransferState.FAILED
        job.retry_count = 5
        job.error_message = "Network timeout"
        db.update(job)

        assert queue.retry_job(job.transfer_id) is True
        retried = queue.get_job(job.transfer_id)
        assert retried.state == TransferState.PENDING_UPLOAD
        assert retried.retry_count == 0
        assert retried.error_message is None
