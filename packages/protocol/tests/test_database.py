"""Tests for SQLite database persistence."""

import pytest

from homevault_protocol.database import TransferDatabase
from homevault_protocol.models import TransferJob, TransferMode, TransferState


@pytest.fixture
def db(tmp_path):
    """Create a fresh in-directory database for each test."""
    database = TransferDatabase(tmp_path / "test.db")
    database.migrate()
    yield database
    database.close()


class TestMigrations:
    def test_migrate_creates_tables(self, tmp_path):
        database = TransferDatabase(tmp_path / "fresh.db")
        database.migrate()
        # Verify the transfers table exists by inserting a job
        job = TransferJob(filename="test.txt")
        database.insert(job)
        assert database.exists(job.transfer_id)
        database.close()

    def test_migrate_is_idempotent(self, tmp_path):
        database = TransferDatabase(tmp_path / "idempotent.db")
        database.migrate()
        database.migrate()  # Should not raise
        database.migrate()  # Still fine
        database.close()


class TestInsert:
    def test_insert_and_retrieve(self, db):
        job = TransferJob(
            mode=TransferMode.COPY,
            filename="document.pdf",
            file_size=1024,
            sha256_hash="abc123",
            source_path="/Users/test/document.pdf",
        )
        db.insert(job)
        retrieved = db.get(job.transfer_id)
        assert retrieved is not None
        assert retrieved.transfer_id == job.transfer_id
        assert retrieved.mode == TransferMode.COPY
        assert retrieved.filename == "document.pdf"
        assert retrieved.file_size == 1024
        assert retrieved.sha256_hash == "abc123"

    def test_duplicate_id_raises(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)
        with pytest.raises(Exception):  # IntegrityError
            db.insert(job)

    def test_insert_move_job(self, db):
        job = TransferJob(mode=TransferMode.MOVE, filename="big.zip")
        db.insert(job)
        retrieved = db.get(job.transfer_id)
        assert retrieved.mode == TransferMode.MOVE


class TestUpdate:
    def test_update_state(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)

        job.state = TransferState.UPLOADING
        job.touch()
        db.update(job)

        retrieved = db.get(job.transfer_id)
        assert retrieved.state == TransferState.UPLOADING

    def test_update_drive_ids(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)

        job.drive_file_id = "drive-file-123"
        job.drive_metadata_id = "drive-meta-456"
        db.update(job)

        retrieved = db.get(job.transfer_id)
        assert retrieved.drive_file_id == "drive-file-123"
        assert retrieved.drive_metadata_id == "drive-meta-456"

    def test_update_error_message(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)

        job.state = TransferState.FAILED
        job.error_message = "Connection timeout"
        db.update(job)

        retrieved = db.get(job.transfer_id)
        assert retrieved.error_message == "Connection timeout"


class TestQueries:
    def test_get_nonexistent(self, db):
        assert db.get("nonexistent-id") is None

    def test_exists(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)
        assert db.exists(job.transfer_id)
        assert not db.exists("nonexistent-id")

    def test_get_by_state(self, db):
        job1 = TransferJob(filename="a.txt", state=TransferState.PENDING_UPLOAD)
        job2 = TransferJob(filename="b.txt", state=TransferState.UPLOADING)
        job3 = TransferJob(filename="c.txt", state=TransferState.PENDING_UPLOAD)
        db.insert(job1)
        db.insert(job2)
        db.insert(job3)

        pending = db.get_by_state(TransferState.PENDING_UPLOAD)
        assert len(pending) == 2
        uploading = db.get_by_state(TransferState.UPLOADING)
        assert len(uploading) == 1

    def test_get_actionable(self, db):
        jobs = [
            TransferJob(filename="a.txt", state=TransferState.PENDING_UPLOAD),
            TransferJob(filename="b.txt", state=TransferState.UPLOADING),
            TransferJob(filename="c.txt", state=TransferState.COMPLETED),
            TransferJob(filename="d.txt", state=TransferState.FAILED),
            TransferJob(filename="e.txt", state=TransferState.QUEUED),
        ]
        for j in jobs:
            db.insert(j)

        actionable = db.get_actionable()
        states = {j.state for j in actionable}
        assert TransferState.PENDING_UPLOAD in states
        assert TransferState.UPLOADING in states
        assert TransferState.QUEUED in states
        assert TransferState.COMPLETED not in states
        assert TransferState.FAILED not in states

    def test_get_all(self, db):
        for i in range(5):
            db.insert(TransferJob(filename=f"file{i}.txt"))
        assert len(db.get_all()) == 5

    def test_count_by_state(self, db):
        db.insert(TransferJob(filename="a.txt", state=TransferState.PENDING_UPLOAD))
        db.insert(TransferJob(filename="b.txt", state=TransferState.PENDING_UPLOAD))
        db.insert(TransferJob(filename="c.txt", state=TransferState.COMPLETED))

        counts = db.count_by_state()
        assert counts["pending_upload"] == 2
        assert counts["completed"] == 1


class TestDelete:
    def test_delete_existing(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)
        assert db.delete(job.transfer_id)
        assert db.get(job.transfer_id) is None

    def test_delete_nonexistent(self, db):
        assert not db.delete("nonexistent-id")


class TestTransaction:
    def test_rollback_on_error(self, db):
        job = TransferJob(filename="test.txt")
        db.insert(job)

        try:
            with db.transaction():
                job.state = TransferState.UPLOADING
                db.update(job)
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass

        # State should be rolled back
        retrieved = db.get(job.transfer_id)
        assert retrieved.state == TransferState.PENDING_UPLOAD
