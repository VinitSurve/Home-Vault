"""Tests for Windows sender CLI commands."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_agent.cli import main
from homevault_protocol.database import TransferDatabase
from homevault_protocol.models import Platform, TransferMode, TransferState


@pytest.fixture
def custom_config(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    db_file = tmp_path / "cli_test_sender.db"
    config_file.write_text(f"""
[sender]
db_path = "{db_file}"
""")
    return config_file, db_file


class TestWindowsCLI:
    def test_send_single_file(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, db_file = custom_config
        sample = tmp_path / "win_hello.txt"
        sample.write_text("Hello Windows HomeVault CLI")

        code = main(["--config", str(config_file), "send", str(sample)])
        assert code == 0

        captured = capsys.readouterr()
        assert "[Queued]" in captured.out
        assert "win_hello.txt" in captured.out
        assert "COPY" in captured.out

        # Verify database record has explicit Windows sender and macOS target
        db = TransferDatabase(db_file)
        jobs = db.get_all()
        db.close()
        assert len(jobs) == 1
        assert jobs[0].sender_platform == Platform.WINDOWS
        assert jobs[0].target_platform == Platform.MACOS

    def test_send_multiple_files_with_move(
        self, custom_config, tmp_path: Path, capsys
    ) -> None:
        config_file, db_file = custom_config
        f1 = tmp_path / "f1.txt"
        f1.write_text("1")
        f2 = tmp_path / "f2.txt"
        f2.write_text("2")

        code = main(
            ["--config", str(config_file), "send", "--mode", "move", str(f1), str(f2)]
        )
        assert code == 0

        captured = capsys.readouterr()
        assert "f1.txt" in captured.out
        assert "f2.txt" in captured.out
        assert "MOVE" in captured.out

        db = TransferDatabase(db_file)
        jobs = db.get_all()
        db.close()
        assert len(jobs) == 2
        for j in jobs:
            assert j.mode == TransferMode.MOVE
            assert j.sender_platform == Platform.WINDOWS
            assert j.target_platform == Platform.MACOS

    def test_copy_shortcut(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, db_file = custom_config
        sample = tmp_path / "copy_win.txt"
        sample.write_text("Copy test")

        code = main(["--config", str(config_file), "copy", str(sample)])
        assert code == 0

        captured = capsys.readouterr()
        assert "COPY" in captured.out
        assert "copy_win.txt" in captured.out

    def test_move_shortcut(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, db_file = custom_config
        sample = tmp_path / "move_win.txt"
        sample.write_text("Move test")

        code = main(["--config", str(config_file), "move", str(sample)])
        assert code == 0

        captured = capsys.readouterr()
        assert "MOVE" in captured.out
        assert "move_win.txt" in captured.out

    def test_send_missing_file_reports_error(self, custom_config, capsys) -> None:
        config_file, _ = custom_config
        code = main(
            ["--config", str(config_file), "send", "C:\\nonexistent_file.xyz"]
        )
        assert code == 1

        captured = capsys.readouterr()
        assert "Error queuing" in captured.err

    def test_send_directory_rejected(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, _ = custom_config
        folder = tmp_path / "folder_item"
        folder.mkdir()

        code = main(["--config", str(config_file), "send", str(folder)])
        assert code == 1

        captured = capsys.readouterr()
        assert "directories are not supported" in captured.err

    def test_status_empty_queue(self, custom_config, capsys) -> None:
        config_file, _ = custom_config
        code = main(["--config", str(config_file), "status"])
        assert code == 0

        captured = capsys.readouterr()
        assert "queue is currently empty" in captured.out

    def test_status_shows_enqueued_jobs(
        self, custom_config, tmp_path: Path, capsys
    ) -> None:
        config_file, _ = custom_config
        f = tmp_path / "status_doc.pdf"
        f.write_bytes(b"PDF data")

        main(["--config", str(config_file), "send", str(f)])
        capsys.readouterr()  # clear buffer

        code = main(["--config", str(config_file), "status"])
        assert code == 0

        captured = capsys.readouterr()
        assert "status_doc.pdf" in captured.out
        assert "pending_upload" in captured.out

    def test_cancel_command(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, db_file = custom_config
        f = tmp_path / "cancel_win.txt"
        f.write_text("Cancel test")

        main(["--config", str(config_file), "send", str(f)])
        capsys.readouterr()

        db = TransferDatabase(db_file)
        job = db.get_all()[0]
        db.close()

        code = main(["--config", str(config_file), "cancel", job.transfer_id])
        assert code == 0
        captured = capsys.readouterr()
        assert "successfully cancelled" in captured.out

    def test_cancel_nonexistent_fails(self, custom_config, capsys) -> None:
        config_file, _ = custom_config
        code = main(["--config", str(config_file), "cancel", "nonexistent-id"])
        assert code == 1
        captured = capsys.readouterr()
        assert "Could not cancel" in captured.err

    def test_retry_command(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, db_file = custom_config
        f = tmp_path / "retry_win.txt"
        f.write_text("Retry test")

        main(["--config", str(config_file), "send", str(f)])
        capsys.readouterr()

        db = TransferDatabase(db_file)
        job = db.get_all()[0]
        job.state = TransferState.FAILED
        job.error_message = "Network error"
        db.update(job)
        db.close()

        code = main(["--config", str(config_file), "retry", job.transfer_id])
        assert code == 0
        captured = capsys.readouterr()
        assert "reset to PENDING_UPLOAD" in captured.out

    def test_retry_non_failed_fails(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, db_file = custom_config
        f = tmp_path / "not_failed.txt"
        f.write_text("Not failed test")

        main(["--config", str(config_file), "send", str(f)])
        capsys.readouterr()

        db = TransferDatabase(db_file)
        job = db.get_all()[0]
        db.close()

        code = main(["--config", str(config_file), "retry", job.transfer_id])
        assert code == 1
        captured = capsys.readouterr()
        assert "Only FAILED or NEEDS_ATTENTION can be retried" in captured.err

    def test_special_character_and_space_paths(
        self, custom_config, tmp_path: Path, capsys
    ) -> None:
        config_file, _ = custom_config
        special_file = tmp_path / "Windows Report (Q3 2026) [Draft].docx"
        special_file.write_text("Document with spaces and symbols")

        code = main(["--config", str(config_file), "send", str(special_file)])
        assert code == 0
        captured = capsys.readouterr()
        assert "[Queued]" in captured.out
        assert "Windows Report (Q3 2026) [Draft].docx" in captured.out

    def test_cli_invalid_mode(self, custom_config, tmp_path: Path, capsys) -> None:
        config_file, _ = custom_config
        sample = tmp_path / "file.txt"
        sample.write_text("test")

        with pytest.raises(SystemExit) as exc_info:
            main(["--config", str(config_file), "send", "--mode", "unsupported", str(sample)])
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err

    def test_worker_command_once(self, custom_config) -> None:
        config_file, _ = custom_config
        with patch("homevault_agent.cli.OAuthManager") as mock_auth_cls:
            mock_auth = MagicMock()
            mock_auth.is_authenticated.return_value = True
            mock_auth.get_credentials.return_value = MagicMock()
            mock_auth_cls.return_value = mock_auth

            with patch("homevault_agent.cli.DriveClient"):
                with patch("homevault_agent.cli.SenderWorker") as mock_worker_cls:
                    mock_worker = MagicMock()
                    mock_worker.run_once.return_value = {"uploaded": 2, "receipted": 1}
                    mock_worker_cls.return_value = mock_worker

                    code = main(["--config", str(config_file), "worker", "--once"])
                    assert code == 0
                    mock_worker.run_once.assert_called_once()
