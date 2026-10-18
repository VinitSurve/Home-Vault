"""Tests for Windows sender SenderWorker daemon."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.queue import SenderQueue
from homevault_agent.sender_worker import SenderWorker
from homevault_agent.staging import StagingManager
from homevault_protocol.config import SenderConfig
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt
from homevault_protocol.models import (
    Platform,
    TransferJob,
    TransferMode,
    TransferState,
)


@pytest.fixture
def db(tmp_path: Path):
    database = TransferDatabase(tmp_path / "sender_worker_test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path: Path) -> SenderConfig:
    cfg = SenderConfig()
    cfg.db_path = str(tmp_path / "sender_worker_test.db")
    cfg.transfer.poll_interval_seconds = 1.0
    return cfg


class TestWindowsSenderWorkerKeyRetrieval:
    def test_explicit_key(self, config: SenderConfig, db: TransferDatabase) -> None:
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            encryption_key="explicit-win-key",
        )
        assert worker.get_encryption_key() == "explicit-win-key"

    def test_env_var_key(self, config: SenderConfig, db: TransferDatabase) -> None:
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {"HOMEVAULT_ENCRYPTION_KEY": "env-win-key"}):
            assert worker.get_encryption_key() == "env-win-key"

    def test_keyring_key(self, config: SenderConfig, db: TransferDatabase) -> None:
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value="keyring-win-key"):
                assert worker.get_encryption_key() == "keyring-win-key"

    def test_missing_key_raises(self, config: SenderConfig, db: TransferDatabase) -> None:
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value=None):
                with pytest.raises(
                    RuntimeError, match="HomeVault encryption key not found in Windows Credential Manager"
                ):
                    worker.get_encryption_key()


class TestWindowsSenderWorkerUploads:
    def test_process_pending_uploads_success(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "upload_win.txt"
        f.write_text("Windows upload content")
        job = queue.enqueue_file(f, mode=TransferMode.COPY)

        staging_mock = MagicMock(spec=StagingManager)

        def mock_stage(j, **kw):
            j.state = TransferState.QUEUED
            j.drive_file_id = "staged-win-id"
            db.update(j)
            return j

        staging_mock.stage_job.side_effect = mock_stage

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
            encryption_key="test-key",
        )

        uploaded = worker.process_pending_uploads()
        assert uploaded == 1
        staging_mock.stage_job.assert_called_once()
        called_job = staging_mock.stage_job.call_args[0][0]
        assert called_job.transfer_id == job.transfer_id
        assert staging_mock.stage_job.call_args[1]["encryption_key"] == "test-key"

    def test_upload_retry_on_transient_failure(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "retry_upload.txt"
        f.write_text("Content")
        job = queue.enqueue_file(f, mode=TransferMode.COPY)

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.stage_job.side_effect = ConnectionError("Google Drive 503")

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
            encryption_key="test-key",
        )

        uploaded = worker.process_pending_uploads()
        assert uploaded == 0

        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.retry_count == 1
        assert saved.state == TransferState.PENDING_UPLOAD

    def test_upload_exceeds_max_retries_marks_failed(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "max_retry.txt"
        f.write_text("Content")
        job = queue.enqueue_file(f, mode=TransferMode.COPY)
        job.retry_count = job.max_retries - 1
        db.update(job)

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.stage_job.side_effect = ConnectionError("Permanent failure")

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
            encryption_key="test-key",
        )

        uploaded = worker.process_pending_uploads()
        assert uploaded == 0

        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.retry_count == job.max_retries
        assert saved.state == TransferState.FAILED


class TestWindowsSenderWorkerReceipts:
    def test_move_job_receipt_finalizes_and_deletes_original(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "to_move.txt"
        f.write_bytes(b"Original file to move to Mac")

        job = queue.enqueue_file(f, mode=TransferMode.MOVE)
        job.state = TransferState.QUEUED
        job.drive_file_id = "blob-move-id"
        db.update(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/to_move.txt",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        staging_manager = StagingManager(drive_client=drive_client, database=db)

        worker = SenderWorker(
            config=config,
            drive_client=drive_client,
            database=db,
            queue=queue,
            staging_manager=staging_manager,
            encryption_key="test-key",
        )

        with patch.object(staging_manager, "check_and_fetch_receipt", return_value=receipt):
            receipted = worker.check_completion_receipts()

        assert receipted == 1
        assert not f.exists()  # Safely deleted!
        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.state == TransferState.COMPLETED
        assert saved.dest_path == "/Users/user/HomeVault Received/to_move.txt"

    def test_move_job_source_modified_triggers_alert(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "tampered.txt"
        f.write_bytes(b"Original data")

        job = queue.enqueue_file(f, mode=TransferMode.MOVE)
        job.state = TransferState.QUEUED
        db.update(job)

        # Modify file on disk after staging
        f.write_bytes(b"TAMPERED DATA!")

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/tampered.txt",
            file_size=len(b"Original data"),
            sha256_hash=job.sha256_hash,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        staging_manager = StagingManager(drive_client=drive_client, database=db)

        worker = SenderWorker(
            config=config,
            drive_client=drive_client,
            database=db,
            queue=queue,
            staging_manager=staging_manager,
            encryption_key="test-key",
        )

        with patch.object(staging_manager, "check_and_fetch_receipt", return_value=receipt):
            with patch("homevault_agent.sender_worker.send_notification") as mock_notify:
                receipted = worker.check_completion_receipts()
                mock_notify.assert_called_with(
                    title="HomeVault Alert",
                    message=f"Source '{job.filename}' was modified after staging or receipt mismatch. Original retained.",
                )

        assert receipted == 0
        assert f.exists()  # Retained!
        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.state == TransferState.NEEDS_ATTENTION

    def test_copy_job_receipt_marks_completed(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "copied.txt"
        f.write_bytes(b"Copy data")

        job = queue.enqueue_file(f, mode=TransferMode.COPY)
        job.state = TransferState.QUEUED
        job.drive_file_id = "blob-copy-id"
        db.update(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/copied.txt",
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        staging_manager = StagingManager(drive_client=drive_client, database=db)

        worker = SenderWorker(
            config=config,
            drive_client=drive_client,
            database=db,
            queue=queue,
            staging_manager=staging_manager,
            encryption_key="test-key",
        )

        with patch.object(staging_manager, "check_and_fetch_receipt", return_value=receipt):
            receipted = worker.check_completion_receipts()

        assert receipted == 1
        assert f.exists()  # Kept!
        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.state == TransferState.COMPLETED
        drive_client.delete_file.assert_called_once_with("blob-copy-id")

    def test_copy_job_receipt_mismatch_marks_needs_attention(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "copy_mismatch.txt"
        f.write_bytes(b"Data")

        job = queue.enqueue_file(f, mode=TransferMode.COPY)
        job.state = TransferState.QUEUED
        db.update(job)

        # Mismatched receipt (wrong hash)
        bad_receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="/Users/user/HomeVault Received/copy_mismatch.txt",
            file_size=job.file_size,
            sha256_hash="0" * 64,
            receiver_platform="macos",
        )

        drive_client = MagicMock(spec=DriveClient)
        staging_manager = StagingManager(drive_client=drive_client, database=db)

        worker = SenderWorker(
            config=config,
            drive_client=drive_client,
            database=db,
            queue=queue,
            staging_manager=staging_manager,
            encryption_key="test-key",
        )

        with patch.object(staging_manager, "check_and_fetch_receipt", return_value=bad_receipt):
            receipted = worker.check_completion_receipts()

        assert receipted == 0
        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.state == TransferState.NEEDS_ATTENTION

    def test_no_receipt_leaves_job_queued(
        self, config: SenderConfig, db: TransferDatabase, tmp_path: Path
    ) -> None:
        queue = SenderQueue(database=db)
        f = tmp_path / "pending_receipt.txt"
        f.write_bytes(b"Data")

        job = queue.enqueue_file(f, mode=TransferMode.COPY)
        job.state = TransferState.QUEUED
        db.update(job)

        drive_client = MagicMock(spec=DriveClient)
        staging_manager = StagingManager(drive_client=drive_client, database=db)

        worker = SenderWorker(
            config=config,
            drive_client=drive_client,
            database=db,
            queue=queue,
            staging_manager=staging_manager,
            encryption_key="test-key",
        )

        with patch.object(staging_manager, "check_and_fetch_receipt", return_value=None):
            receipted = worker.check_completion_receipts()

        assert receipted == 0
        saved = db.get(job.transfer_id)
        assert saved is not None
        assert saved.state == TransferState.QUEUED

    def test_run_once(self, config: SenderConfig, db: TransferDatabase) -> None:
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            encryption_key="test-key",
        )
        res = worker.run_once()
        assert res == {"uploaded": 0, "receipted": 0}

    def test_run_forever_graceful_stop(self, config: SenderConfig, db: TransferDatabase) -> None:
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            encryption_key="test-key",
        )

        def stop_after_one_loop():
            worker.stop()

        with patch.object(worker, "process_pending_uploads", side_effect=stop_after_one_loop):
            worker.run_forever()

        assert worker._running is False
