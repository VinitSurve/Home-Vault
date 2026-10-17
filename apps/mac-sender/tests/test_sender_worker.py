"""Tests for macOS sender SenderWorker daemon."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_protocol.config import SenderConfig
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.hashing import hash_file
from homevault_protocol.metadata import CompletionReceipt
from homevault_protocol.models import TransferMode, TransferState
from homevault_sender.queue import SenderQueue
from homevault_sender.staging import StagingManager
from homevault_sender.worker import SenderWorker


@pytest.fixture
def db(tmp_path):
    database = TransferDatabase(tmp_path / "sender_worker_test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    cfg = SenderConfig()
    cfg.db_path = str(tmp_path / "sender_worker_test.db")
    cfg.transfer.poll_interval_seconds = 1.0
    return cfg


class TestSenderWorkerKeyRetrieval:
    def test_explicit_key(self, config, db):
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            encryption_key="explicit-mac-key",
        )
        assert worker.get_encryption_key() == "explicit-mac-key"

    def test_env_var_key(self, config, db):
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {"HOMEVAULT_ENCRYPTION_KEY": "env-mac-key"}):
            assert worker.get_encryption_key() == "env-mac-key"

    def test_keyring_key(self, config, db):
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value="keyring-mac-key"):
                assert worker.get_encryption_key() == "keyring-mac-key"

    def test_missing_key_raises(self, config, db):
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value=None):
                with pytest.raises(RuntimeError, match="HomeVault encryption key not found in macOS Keychain"):
                    worker.get_encryption_key()


class TestSenderWorkerUploads:
    def test_process_pending_uploads_success(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        f = tmp_path / "upload_me.txt"
        f.write_text("Upload content")
        job = queue.enqueue_file(f, mode=TransferMode.COPY)

        staging_mock = MagicMock(spec=StagingManager)
        # Simulate successful staging
        def mock_stage(j, **kw):
            j.state = TransferState.QUEUED
            j.drive_file_id = "staged-blob-id"
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

        with patch("homevault_sender.worker.send_notification") as mock_notif:
            count = worker.process_pending_uploads()
            assert count == 1
            staging_mock.stage_job.assert_called_once()
            mock_notif.assert_called_once()
            assert "Uploaded" in mock_notif.call_args[1]["title"]

        # Job in database is now QUEUED
        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.QUEUED

    def test_upload_retry_on_transient_failure(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        f = tmp_path / "transient.txt"
        f.write_text("Retry content")
        job = queue.enqueue_file(f)

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.stage_job.side_effect = ConnectionError("Drive API temporarily unreachable")

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
            encryption_key="test-key",
        )

        count = worker.process_pending_uploads()
        assert count == 0

        # Job remains in PENDING_UPLOAD with incremented retry count
        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.PENDING_UPLOAD
        assert updated.retry_count == 1

    def test_upload_exceeds_max_retries_marks_failed(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        f = tmp_path / "failing.txt"
        f.write_text("Fail content")
        job = queue.enqueue_file(f)
        job.retry_count = 4  # one attempt left
        job.max_retries = 5
        db.update(job)

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.stage_job.side_effect = ConnectionError("Hard network outage")

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
            encryption_key="test-key",
        )

        count = worker.process_pending_uploads()
        assert count == 0

        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.FAILED
        assert updated.retry_count == 5


class TestSenderWorkerReceipts:
    def test_move_job_receipt_finalizes_and_deletes_original(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        source = tmp_path / "move_file.txt"
        source.write_text("Move file data")
        expected_hash = hash_file(source)

        job = queue.enqueue_file(source, mode=TransferMode.MOVE)
        job.state = TransferState.QUEUED
        job.drive_file_id = "cloud-staged-id"
        db.update(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="D:\\HomeVault\\Inbox\\move_file.txt",
            file_size=source.stat().st_size,
            sha256_hash=expected_hash,
        )

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.check_and_fetch_receipt.return_value = receipt
        staging_mock.finalize_move.return_value = True

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
        )

        with patch("homevault_sender.worker.send_notification") as mock_notif:
            count = worker.check_completion_receipts()
            assert count == 1
            staging_mock.finalize_move.assert_called_once_with(job, receipt, delete_cloud_staging=True)
            mock_notif.assert_called_once()
            assert "Move Completed" in mock_notif.call_args[1]["title"]

    def test_move_job_source_modified_triggers_alert(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        source = tmp_path / "modified_move.txt"
        source.write_text("Initial content")

        job = queue.enqueue_file(source, mode=TransferMode.MOVE)
        job.state = TransferState.QUEUED
        db.update(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="D:\\HomeVault\\Inbox\\modified_move.txt",
            file_size=100,
            sha256_hash="hash",
        )

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.check_and_fetch_receipt.return_value = receipt
        staging_mock.finalize_move.return_value = False  # Source changed!

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
        )

        with patch("homevault_sender.worker.send_notification") as mock_notif:
            count = worker.check_completion_receipts()
            assert count == 0
            mock_notif.assert_called_once()
            assert "Alert" in mock_notif.call_args[1]["title"]

    def test_copy_job_receipt_marks_completed(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        source = tmp_path / "copy_file.txt"
        source.write_text("Copy file data")

        job = queue.enqueue_file(source, mode=TransferMode.COPY)
        job.state = TransferState.QUEUED
        job.drive_file_id = "copy-cloud-id"
        db.update(job)

        receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="D:\\HomeVault\\Inbox\\copy_file.txt",
            file_size=source.stat().st_size,
            sha256_hash=hash_file(source),
        )

        drive_mock = MagicMock(spec=DriveClient)
        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.check_and_fetch_receipt.return_value = receipt

        worker = SenderWorker(
            config=config,
            drive_client=drive_mock,
            database=db,
            queue=queue,
            staging_manager=staging_mock,
        )

        with patch("homevault_sender.worker.send_notification") as mock_notif:
            count = worker.check_completion_receipts()
            assert count == 1
            mock_notif.assert_called_once()
            assert "Copy Delivered" in mock_notif.call_args[1]["title"]

        # Original source file is preserved!
        assert source.exists()
        # Job marked COMPLETED in database
        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.COMPLETED
        # Staged cloud blob purged
        drive_mock.delete_file.assert_called_once_with("copy-cloud-id")

    def test_run_once(self, config, db):
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=MagicMock(spec=SenderQueue, get_pending_uploads=lambda: [], get_queued_transfers=lambda: []),
        )
        res = worker.run_once()
        assert res == {"uploaded": 0, "receipted": 0}

    def test_run_forever_graceful_stop(self, config, db):
        queue_mock = MagicMock(spec=SenderQueue)

        def mock_pending():
            worker.stop()
            return []

        queue_mock.get_pending_uploads.side_effect = mock_pending
        queue_mock.get_queued_transfers.return_value = []

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue_mock,
        )

        worker.run_forever()
        assert not worker._running

    def test_copy_job_receipt_mismatch_marks_needs_attention(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        source = tmp_path / "tampered_copy.txt"
        source.write_text("Copy data")

        job = queue.enqueue_file(source, mode=TransferMode.COPY)
        job.state = TransferState.QUEUED
        job.drive_file_id = "copy-cloud-id"
        db.update(job)

        # Receipt with wrong SHA-256 hash
        bad_receipt = CompletionReceipt(
            transfer_id=job.transfer_id,
            final_path="D:\\HomeVault\\Inbox\\tampered_copy.txt",
            file_size=source.stat().st_size,
            sha256_hash="0000000000000000000000000000000000000000000000000000000000000000",
        )

        staging_mock = MagicMock(spec=StagingManager)
        staging_mock.check_and_fetch_receipt.return_value = bad_receipt

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
        )

        with patch("homevault_sender.worker.send_notification") as mock_notif:
            count = worker.check_completion_receipts()
            assert count == 0
            mock_notif.assert_called_once()
            assert "Alert" in mock_notif.call_args[1]["title"]

        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.NEEDS_ATTENTION
        assert "metadata mismatch" in updated.error_message or "does not match" in updated.error_message

    def test_interrupted_upload_resumed(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        source = tmp_path / "resumed_upload.txt"
        source.write_text("Resume data")

        # Job was left in UPLOADING state due to previous crash or shutdown
        job = queue.enqueue_file(source, mode=TransferMode.COPY)
        job.state = TransferState.UPLOADING
        db.update(job)

        staging_mock = MagicMock(spec=StagingManager)
        def mock_stage(j, **kw):
            j.state = TransferState.QUEUED
            j.drive_file_id = "resumed-cloud-id"
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

        count = worker.process_pending_uploads()
        assert count == 1
        updated = queue.get_job(job.transfer_id)
        assert updated.state == TransferState.QUEUED
        assert updated.drive_file_id == "resumed-cloud-id"

    def test_expired_credentials_logs_error_without_crashing(self, config, db, tmp_path):
        queue = SenderQueue(database=db)
        source = tmp_path / "expired_token.txt"
        source.write_text("Data")
        job = queue.enqueue_file(source)

        staging_mock = MagicMock(spec=StagingManager)
        # Simulate OAuth token expired error
        from google.auth.exceptions import RefreshError
        staging_mock.stage_job.side_effect = RefreshError("Token has been expired or revoked.")

        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            queue=queue,
            staging_manager=staging_mock,
            encryption_key="test-key",
        )

        # process_pending_uploads catches error and records retry
        count = worker.process_pending_uploads()
        assert count == 0
        updated = queue.get_job(job.transfer_id)
        assert updated.retry_count == 1
        assert "Token has been expired or revoked" in updated.error_message

    def test_worker_signal_shutdown(self, config, db):
        worker = SenderWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        worker._install_signal_handlers()
        assert not worker._running
        # Simulate signal
        worker._running = True
        worker.stop()
        assert not worker._running
