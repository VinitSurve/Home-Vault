import sqlite3

import pytest

from homevault_protocol.database import _MIGRATIONS, TransferDatabase
from homevault_protocol.models import (
    Platform,
    TransferJob,
    TransferMode,
    TransferState,
)


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

    def test_migration_v1_to_v2_with_existing_data(self, tmp_path):
        db_path = tmp_path / "legacy_v1.db"

        # Manually create a v1 schema database with raw sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_MIGRATIONS[1])
        # Insert a legacy v1 transfer record without sender_platform or target_platform
        conn.execute(
            """
            INSERT INTO transfers (
                transfer_id, mode, state, source_path, filename,
                file_size, sha256_hash, created_at, updated_at
            ) VALUES (
                'legacy-uuid-1', 'copy', 'completed', '/tmp/old.txt', 'old.txt',
                128, 'abc123hash', '2024-01-01T00:00:00Z', '2024-01-01T00:01:00Z'
            )
            """
        )
        conn.commit()
        conn.close()

        # Open with TransferDatabase and run migration to v2
        db = TransferDatabase(db_path)
        assert db._get_version() == 1

        db.migrate()
        assert db._get_version() == 2

        # Verify legacy row is preserved with safe default platforms
        legacy_job = db.get("legacy-uuid-1")
        assert legacy_job is not None
        assert legacy_job.filename == "old.txt"
        assert legacy_job.sender_platform == Platform.MACOS
        assert legacy_job.target_platform == Platform.WINDOWS

        # Verify a new job with Windows sender can be inserted
        win_job = TransferJob(
            filename="win_file.zip",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        db.insert(win_job)

        retrieved_win = db.get(win_job.transfer_id)
        assert retrieved_win is not None
        assert retrieved_win.sender_platform == Platform.WINDOWS
        assert retrieved_win.target_platform == Platform.MACOS

        # Verify running migrate again is safe and idempotent
        db.migrate()
        assert db._get_version() == 2
        db.close()


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

    def test_get_by_target(self, db):
        job1 = TransferJob(
            filename="to_win.txt",
            sender_platform=Platform.MACOS,
            target_platform=Platform.WINDOWS,
            state=TransferState.PENDING_UPLOAD,
        )
        job2 = TransferJob(
            filename="to_mac.txt",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
            state=TransferState.QUEUED,
        )
        job3 = TransferJob(
            filename="to_win_2.txt",
            sender_platform=Platform.MACOS,
            target_platform=Platform.WINDOWS,
            state=TransferState.COMPLETED,
        )
        db.insert(job1)
        db.insert(job2)
        db.insert(job3)

        windows_jobs = db.get_by_target(Platform.WINDOWS)
        assert len(windows_jobs) == 2
        assert {j.filename for j in windows_jobs} == {"to_win.txt", "to_win_2.txt"}

        macos_jobs = db.get_by_target(Platform.MACOS)
        assert len(macos_jobs) == 1
        assert macos_jobs[0].filename == "to_mac.txt"

        # Test filtering by both target and state (using enum)
        pending_win = db.get_by_target(
            Platform.WINDOWS, state=TransferState.PENDING_UPLOAD
        )
        assert len(pending_win) == 1
        assert pending_win[0].filename == "to_win.txt"

        # Test filtering using string platform and string state
        pending_win_str = db.get_by_target("windows", state="pending_upload")
        assert len(pending_win_str) == 1
        assert pending_win_str[0].filename == "to_win.txt"

    def test_get_by_sender(self, db):
        job_mac = TransferJob(
            filename="from_mac.txt",
            sender_platform=Platform.MACOS,
            target_platform=Platform.WINDOWS,
        )
        job_win = TransferJob(
            filename="from_win.txt",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        db.insert(job_mac)
        db.insert(job_win)

        mac_sent = db.get_by_sender(Platform.MACOS)
        assert len(mac_sent) == 1
        assert mac_sent[0].filename == "from_mac.txt"

        win_sent = db.get_by_sender(Platform.WINDOWS)
        assert len(win_sent) == 1
        assert win_sent[0].filename == "from_win.txt"

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
