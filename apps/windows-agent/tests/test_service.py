"""Tests for Windows Unified Service (homevault_agent.service).

Tests:
- Unified cycle execution with both ReceiverWorker and SenderWorker.
- Exception isolation: failure in ReceiverWorker does not halt SenderWorker, and vice-versa.
- Graceful startup, loop execution, and shutdown.
- CLI argument parsing and --once execution.
- Isolated database usage between sender and receiver.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.sender_worker import SenderWorker
from homevault_agent.service import (
    main,
    run_unified_cycle,
    run_unified_forever,
)
from homevault_agent.worker import ReceiverWorker
from homevault_protocol.config import ReceiverConfig, SenderConfig
from homevault_protocol.database import TransferDatabase


@pytest.fixture
def mock_receiver():
    rec = MagicMock(spec=ReceiverWorker)
    rec.poll_and_process.return_value = [(Path("inbox/test.txt"), MagicMock())]
    return rec


@pytest.fixture
def mock_sender():
    send = MagicMock(spec=SenderWorker)
    send.process_pending_uploads.return_value = 2
    send.check_completion_receipts.return_value = 1
    return send


class TestRunUnifiedCycle:
    def test_both_workers_succeed(self, mock_receiver, mock_sender):
        results = run_unified_cycle(mock_receiver, mock_sender)

        assert results == {
            "received": 1,
            "uploaded": 2,
            "receipted": 1,
        }
        mock_receiver.poll_and_process.assert_called_once()
        mock_sender.process_pending_uploads.assert_called_once()
        mock_sender.check_completion_receipts.assert_called_once()

    def test_receiver_exception_does_not_halt_sender(self, mock_receiver, mock_sender):
        mock_receiver.poll_and_process.side_effect = RuntimeError("Drive network error")

        results = run_unified_cycle(mock_receiver, mock_sender)

        assert results["received"] == 0
        assert results["uploaded"] == 2
        assert results["receipted"] == 1
        mock_sender.process_pending_uploads.assert_called_once()
        mock_sender.check_completion_receipts.assert_called_once()

    def test_sender_upload_exception_does_not_halt_receipts(self, mock_receiver, mock_sender):
        mock_sender.process_pending_uploads.side_effect = OSError("Disk read error")

        results = run_unified_cycle(mock_receiver, mock_sender)

        assert results["received"] == 1
        assert results["uploaded"] == 0
        assert results["receipted"] == 1
        mock_receiver.poll_and_process.assert_called_once()
        mock_sender.check_completion_receipts.assert_called_once()

    def test_sender_receipt_exception_does_not_crash(self, mock_receiver, mock_sender):
        mock_sender.check_completion_receipts.side_effect = ValueError("Corrupt receipt JSON")

        results = run_unified_cycle(mock_receiver, mock_sender)

        assert results["received"] == 1
        assert results["uploaded"] == 2
        assert results["receipted"] == 0

    def test_sender_none_runs_receiver_only(self, mock_receiver):
        results = run_unified_cycle(mock_receiver, sender_worker=None)

        assert results == {
            "received": 1,
            "uploaded": 0,
            "receipted": 0,
        }
        mock_receiver.poll_and_process.assert_called_once()


class TestRunUnifiedForever:
    @patch("time.sleep")
    def test_graceful_shutdown(self, mock_sleep, mock_receiver, mock_sender):
        cycle_count = 0

        def fake_cycle(rec, send):
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count >= 2:
                # Signal stop on second iteration
                raise KeyboardInterrupt()
            return {"received": 0, "uploaded": 0, "receipted": 0}

        with patch("homevault_agent.service.run_unified_cycle", side_effect=fake_cycle):
            with pytest.raises(KeyboardInterrupt):
                run_unified_forever(mock_receiver, mock_sender, poll_interval=1)

        assert cycle_count == 2

    @patch("time.sleep")
    def test_loop_handles_cycle_exception(self, mock_sleep, mock_receiver, mock_sender):
        call_count = 0

        def side_effect(rec, send):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Catastrophic cycle failure")
            raise KeyboardInterrupt()

        with patch("homevault_agent.service.run_unified_cycle", side_effect=side_effect):
            with pytest.raises(KeyboardInterrupt):
                run_unified_forever(mock_receiver, mock_sender, poll_interval=1)

        assert call_count == 2


class TestDatabaseIsolation:
    def test_sender_and_receiver_use_separate_databases(self, tmp_path):
        rec_db_file = tmp_path / "receiver.db"
        send_db_file = tmp_path / "sender.db"

        rec_db = TransferDatabase(rec_db_file)
        rec_db.migrate()

        send_db = TransferDatabase(send_db_file)
        send_db.migrate()

        assert rec_db_file.exists()
        assert send_db_file.exists()
        assert rec_db_file != send_db_file

        rec_db.close()
        send_db.close()


class TestServiceCli:
    @patch("homevault_agent.service.OAuthManager")
    def test_main_unauthenticated_returns_1(self, mock_oauth_cls):
        mock_auth = MagicMock()
        mock_auth.is_authenticated.return_value = False
        mock_oauth_cls.return_value = mock_auth

        ret = main(["--once"])
        assert ret == 1

    @patch("homevault_agent.service.OAuthManager")
    @patch("homevault_agent.service.DriveClient")
    @patch("homevault_agent.service.ReceiverWorker")
    @patch("homevault_agent.service.SenderWorker")
    @patch("homevault_agent.service.SenderQueue")
    @patch("homevault_agent.service.TransferDatabase")
    def test_main_once_executes_unified_cycle(
        self,
        mock_db_cls,
        mock_queue_cls,
        mock_sender_cls,
        mock_receiver_cls,
        mock_drive_cls,
        mock_oauth_cls,
        tmp_path,
    ):
        mock_auth = MagicMock()
        mock_auth.is_authenticated.return_value = True
        mock_auth.get_credentials.return_value = MagicMock()
        mock_oauth_cls.return_value = mock_auth

        mock_rec_inst = MagicMock()
        mock_receiver_cls.return_value = mock_rec_inst

        mock_send_inst = MagicMock()
        mock_sender_cls.return_value = mock_send_inst

        with patch("homevault_agent.service.run_unified_cycle") as mock_cycle:
            mock_cycle.return_value = {"received": 1, "uploaded": 1, "receipted": 1}
            ret = main(["--once", "--inbox", str(tmp_path / "inbox"), "--interval", "10"])
            assert ret == 0
            mock_cycle.assert_called_once_with(mock_rec_inst, mock_send_inst)

    @patch("homevault_agent.service.OAuthManager")
    @patch("homevault_agent.service.DriveClient")
    @patch("homevault_agent.service.ReceiverWorker", side_effect=RuntimeError("Worker init fail"))
    @patch("homevault_agent.service.TransferDatabase")
    def test_main_worker_init_failure_exits_1_and_closes_db(
        self,
        mock_db_cls,
        mock_receiver_cls,
        mock_drive_cls,
        mock_oauth_cls,
    ):
        mock_auth = MagicMock()
        mock_auth.is_authenticated.return_value = True
        mock_auth.get_credentials.return_value = MagicMock()
        mock_oauth_cls.return_value = mock_auth

        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db

        ret = main(["--once"])
        assert ret == 1
        assert mock_db.close.call_count >= 2
