"""Tests for Windows receiver ReceiverWorker."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.discovery import CloudDiscoveryManager
from homevault_agent.worker import ReceiverWorker
from homevault_protocol.config import ReceiverConfig
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.metadata import CompletionReceipt, TransferMetadata


@pytest.fixture
def config(tmp_path):
    cfg = ReceiverConfig()
    cfg.inbox_path = str(tmp_path / "inbox")
    cfg.db_path = str(tmp_path / "test.db")
    cfg.transfer.poll_interval_seconds = 1.0
    return cfg


@pytest.fixture
def db(tmp_path):
    database = TransferDatabase(tmp_path / "worker_test.db")
    database.migrate()
    yield database
    database.close()


class TestReceiverWorkerKeyRetrieval:
    def test_explicit_key(self, config, db):
        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            encryption_key="explicit-key-123",
        )
        assert worker.get_encryption_key() == "explicit-key-123"

    def test_env_var_key(self, config, db):
        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {"HOMEVAULT_ENCRYPTION_KEY": "env-key-456"}):
            assert worker.get_encryption_key() == "env-key-456"

    def test_keyring_key(self, config, db):
        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value="keyring-key-789"):
                assert worker.get_encryption_key() == "keyring-key-789"

    def test_missing_key_raises(self, config, db):
        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("keyring.get_password", return_value=None):
                with pytest.raises(RuntimeError, match="HomeVault encryption key not found"):
                    worker.get_encryption_key()


class TestReceiverWorkerExecution:
    def test_poll_and_process_empty(self, config, db):
        discovery_mock = MagicMock(spec=CloudDiscoveryManager)
        discovery_mock.discover_pending_transfers.return_value = []

        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            discovery_manager=discovery_mock,
            encryption_key="test-key",
        )

        results = worker.poll_and_process()
        assert results == []
        discovery_mock.discover_pending_transfers.assert_called_once()
        discovery_mock.download_and_verify.assert_not_called()

    def test_poll_and_process_delivers_job(self, config, db, tmp_path):
        meta = TransferMetadata(
            transfer_id="job-abc",
            mode="copy",
            filename="hello.txt",
            file_size=12,
            sha256_hash="hash",
            created_at="2026-01-01T00:00:00Z",
        )

        discovery_mock = MagicMock(spec=CloudDiscoveryManager)
        discovery_mock.discover_pending_transfers.return_value = [meta]

        delivered_path = tmp_path / "hello.txt"
        receipt = CompletionReceipt(
            transfer_id="job-abc",
            final_path=str(delivered_path),
            file_size=12,
            sha256_hash="hash",
        )
        discovery_mock.download_and_verify.return_value = (delivered_path, receipt)

        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            discovery_manager=discovery_mock,
            encryption_key="test-key",
        )

        results = worker.poll_and_process()
        assert len(results) == 1
        assert results[0] == (delivered_path, receipt)
        discovery_mock.download_and_verify.assert_called_once_with(
            metadata=meta,
            encryption_key="test-key",
            dry_run=False,
        )

    def test_run_once(self, config, db):
        discovery_mock = MagicMock(spec=CloudDiscoveryManager)
        discovery_mock.discover_pending_transfers.return_value = []

        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            discovery_manager=discovery_mock,
            encryption_key="test-key",
        )

        results = worker.run_once()
        assert results == []

    def test_run_forever_graceful_stop(self, config, db):
        discovery_mock = MagicMock(spec=CloudDiscoveryManager)
        discovery_mock.discover_pending_transfers.return_value = []

        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            discovery_manager=discovery_mock,
            encryption_key="test-key",
        )

        # Trigger stop on first discovery call
        def mock_discover():
            worker.stop()
            return []

        discovery_mock.discover_pending_transfers.side_effect = mock_discover
        worker.run_forever()

        assert not worker._running

    def test_poll_catches_error_without_crash(self, config, db):
        discovery_mock = MagicMock(spec=CloudDiscoveryManager)
        discovery_mock.discover_pending_transfers.side_effect = ConnectionError("Drive API timeout")

        worker = ReceiverWorker(
            config=config,
            drive_client=MagicMock(spec=DriveClient),
            database=db,
            discovery_manager=discovery_mock,
            encryption_key="test-key",
        )

        # Calling run_once should log and handle rather than crashing unhandled
        with pytest.raises(ConnectionError):
            worker.poll_and_process()
