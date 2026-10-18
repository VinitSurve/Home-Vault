"""Tests for Windows sender SenderQueue."""

from pathlib import Path

import pytest

from homevault_agent.queue import SenderQueue
from homevault_protocol.database import TransferDatabase
from homevault_protocol.models import Platform, TransferJob, TransferMode, TransferState


@pytest.fixture
def temp_db(tmp_path: Path) -> TransferDatabase:
    db = TransferDatabase(tmp_path / "windows_sender_test.db")
    db.migrate()
    return db


@pytest.fixture
def queue(temp_db: TransferDatabase) -> SenderQueue:
    return SenderQueue(temp_db)


class TestWindowsSenderQueue:
    def test_enqueue_file_copy(self, queue: SenderQueue, tmp_path: Path) -> None:
        source = tmp_path / "document.docx"
        source.write_bytes(b"Windows file contents")

        job = queue.enqueue_file(source, mode=TransferMode.COPY)

        assert job.mode == TransferMode.COPY
        assert job.state == TransferState.PENDING_UPLOAD
        assert job.sender_platform == Platform.WINDOWS
        assert job.target_platform == Platform.MACOS
        assert job.filename == "document.docx"
        assert job.file_size == len(b"Windows file contents")
        assert len(job.sha256_hash) == 64

        # Verify persisted in database
        saved = queue.get_job(job.transfer_id)
        assert saved is not None
        assert saved.sender_platform == Platform.WINDOWS
        assert saved.target_platform == Platform.MACOS

    def test_enqueue_file_move(self, queue: SenderQueue, tmp_path: Path) -> None:
        source = tmp_path / "move_me.txt"
        source.write_bytes(b"Move this file")

        job = queue.enqueue_file(source, mode=TransferMode.MOVE)

        assert job.mode == TransferMode.MOVE
        assert job.sender_platform == Platform.WINDOWS
        assert job.target_platform == Platform.MACOS

    def test_enqueue_file_custom_target(self, queue: SenderQueue, tmp_path: Path) -> None:
        source = tmp_path / "custom.txt"
        source.write_bytes(b"Custom target test")

        job = queue.enqueue_file(source, target_platform="macos")
        assert job.target_platform == Platform.MACOS

        # Reject same-platform target
        with pytest.raises(ValueError, match="Target platform cannot be Windows"):
            queue.enqueue_file(source, target_platform=Platform.WINDOWS)

    def test_enqueue_missing_file_raises(self, queue: SenderQueue, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.txt"
        with pytest.raises(FileNotFoundError):
            queue.enqueue_file(missing)

    def test_enqueue_directory_raises(self, queue: SenderQueue, tmp_path: Path) -> None:
        folder = tmp_path / "some_folder"
        folder.mkdir()
        with pytest.raises(ValueError, match="directories are not supported"):
            queue.enqueue_file(folder)

    def test_queue_persistence_across_instances(self, tmp_path: Path) -> None:
        db_path = tmp_path / "persistent.db"
        db1 = TransferDatabase(db_path)
        q1 = SenderQueue(db1)

        source = tmp_path / "sample.pdf"
        source.write_bytes(b"Data")
        job = q1.enqueue_file(source)

        db2 = TransferDatabase(db_path)
        q2 = SenderQueue(db2)
        recovered = q2.get_job(job.transfer_id)

        assert recovered is not None
        assert recovered.transfer_id == job.transfer_id
        assert recovered.sender_platform == Platform.WINDOWS
        assert recovered.target_platform == Platform.MACOS

    def test_get_pending_uploads_filters_sender_and_state(
        self, queue: SenderQueue, temp_db: TransferDatabase, tmp_path: Path
    ) -> None:
        # 1. Windows job in PENDING_UPLOAD
        src1 = tmp_path / "f1.txt"
        src1.write_bytes(b"1")
        j1 = queue.enqueue_file(src1)

        # 2. Windows job in UPLOADING
        src2 = tmp_path / "f2.txt"
        src2.write_bytes(b"2")
        j2 = queue.enqueue_file(src2)
        j2.state = TransferState.UPLOADING
        temp_db.update(j2)

        # 3. Windows job in QUEUED (already uploaded)
        src3 = tmp_path / "f3.txt"
        src3.write_bytes(b"3")
        j3 = queue.enqueue_file(src3)
        j3.state = TransferState.QUEUED
        temp_db.update(j3)

        # 4. Incoming Mac job in PENDING_UPLOAD (should NOT be returned by Windows sender queue)
        mac_job = TransferJob(
            sender_platform=Platform.MACOS,
            target_platform=Platform.WINDOWS,
            state=TransferState.PENDING_UPLOAD,
            filename="incoming_from_mac.txt",
        )
        temp_db.insert(mac_job)

        pending = queue.get_pending_uploads()
        pending_ids = {j.transfer_id for j in pending}

        assert j1.transfer_id in pending_ids
        assert j2.transfer_id in pending_ids
        assert j3.transfer_id not in pending_ids
        assert mac_job.transfer_id not in pending_ids

    def test_get_queued_transfers(
        self, queue: SenderQueue, temp_db: TransferDatabase, tmp_path: Path
    ) -> None:
        src = tmp_path / "queued_file.txt"
        src.write_bytes(b"queued")
        job = queue.enqueue_file(src)
        job.state = TransferState.QUEUED
        temp_db.update(job)

        # Incoming Mac queued job
        mac_job = TransferJob(
            sender_platform=Platform.MACOS,
            target_platform=Platform.WINDOWS,
            state=TransferState.QUEUED,
            filename="mac_queued.txt",
        )
        temp_db.insert(mac_job)

        queued = queue.get_queued_transfers()
        queued_ids = {j.transfer_id for j in queued}

        assert job.transfer_id in queued_ids
        assert mac_job.transfer_id not in queued_ids

    def test_cancel_pending_job(self, queue: SenderQueue, tmp_path: Path) -> None:
        src = tmp_path / "cancel_me.txt"
        src.write_bytes(b"content")
        job = queue.enqueue_file(src)

        assert queue.cancel_job(job.transfer_id) is True
        updated = queue.get_job(job.transfer_id)
        assert updated is not None
        assert updated.state == TransferState.CANCELLED

        # Cannot cancel already cancelled job
        assert queue.cancel_job(job.transfer_id) is False

    def test_retry_failed_job(
        self, queue: SenderQueue, temp_db: TransferDatabase, tmp_path: Path
    ) -> None:
        src = tmp_path / "retry_me.txt"
        src.write_bytes(b"content")
        job = queue.enqueue_file(src)

        job.state = TransferState.FAILED
        job.retry_count = 3
        job.error_message = "Network timeout"
        temp_db.update(job)

        assert queue.retry_job(job.transfer_id) is True
        updated = queue.get_job(job.transfer_id)
        assert updated is not None
        assert updated.state == TransferState.PENDING_UPLOAD
        assert updated.retry_count == 0
        assert updated.error_message is None

        # Cannot retry job in non-failed state
        assert queue.retry_job(job.transfer_id) is False

    def test_get_all_jobs(self, queue: SenderQueue, tmp_path: Path) -> None:
        for i in range(3):
            f = tmp_path / f"file_{i}.txt"
            f.write_bytes(f"test {i}".encode())
            queue.enqueue_file(f)

        jobs = queue.get_all_jobs(limit=2)
        assert len(jobs) == 2
