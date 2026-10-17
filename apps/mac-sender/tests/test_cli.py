"""Tests for macOS sender CLI commands."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevault_protocol.models import TransferMode, TransferState
from homevault_sender.cli import main


@pytest.fixture
def custom_config(tmp_path):
    config_file = tmp_path / "config.toml"
    db_file = tmp_path / "cli_test_sender.db"
    config_file.write_text(f"""
[sender]
db_path = "{db_file}"
""")
    return config_file, db_file


class TestCLI:
    def test_send_single_file(self, custom_config, tmp_path, capsys):
        config_file, db_file = custom_config
        sample = tmp_path / "hello.txt"
        sample.write_text("Hello HomeVault CLI")

        code = main(["--config", str(config_file), "send", str(sample)])
        assert code == 0

        captured = capsys.readouterr()
        assert "[Queued]" in captured.out
        assert "hello.txt" in captured.out
        assert "COPY" in captured.out

    def test_send_multiple_files_with_move(self, custom_config, tmp_path, capsys):
        config_file, db_file = custom_config
        f1 = tmp_path / "file1.txt"
        f1.write_text("1")
        f2 = tmp_path / "file2.txt"
        f2.write_text("2")

        code = main(["--config", str(config_file), "send", "--mode", "move", str(f1), str(f2)])
        assert code == 0

        captured = capsys.readouterr()
        assert "file1.txt" in captured.out
        assert "file2.txt" in captured.out
        assert "MOVE" in captured.out

    def test_copy_shortcut(self, custom_config, tmp_path, capsys):
        config_file, db_file = custom_config
        sample = tmp_path / "copy_me.txt"
        sample.write_text("Copy test")

        code = main(["--config", str(config_file), "copy", str(sample)])
        assert code == 0

        captured = capsys.readouterr()
        assert "COPY" in captured.out
        assert "copy_me.txt" in captured.out

    def test_move_shortcut(self, custom_config, tmp_path, capsys):
        config_file, db_file = custom_config
        sample = tmp_path / "move_me.txt"
        sample.write_text("Move test")

        code = main(["--config", str(config_file), "move", str(sample)])
        assert code == 0

        captured = capsys.readouterr()
        assert "MOVE" in captured.out
        assert "move_me.txt" in captured.out

    def test_send_missing_file_reports_error(self, custom_config, capsys):
        config_file, _ = custom_config
        code = main(["--config", str(config_file), "send", "/path/to/nonexistent_file.xyz"])
        assert code == 1

        captured = capsys.readouterr()
        assert "Error queuing" in captured.err

    def test_status_empty_queue(self, custom_config, capsys):
        config_file, _ = custom_config
        code = main(["--config", str(config_file), "status"])
        assert code == 0

        captured = capsys.readouterr()
        assert "queue is currently empty" in captured.out

    def test_status_shows_enqueued_jobs(self, custom_config, tmp_path, capsys):
        config_file, _ = custom_config
        f = tmp_path / "test_status.pdf"
        f.write_bytes(b"PDF content")

        main(["--config", str(config_file), "send", str(f)])
        capsys.readouterr()  # clear buffer

        code = main(["--config", str(config_file), "status"])
        assert code == 0

        captured = capsys.readouterr()
        assert "test_status.pdf" in captured.out
        assert "pending_upload" in captured.out

    def test_cancel_command(self, custom_config, tmp_path, capsys):
        config_file, _ = custom_config
        f = tmp_path / "test_cancel.txt"
        f.write_text("Cancel test")

        main(["--config", str(config_file), "send", str(f)])
        out = capsys.readouterr().out
        # Extract transfer ID from "[Queued] <ID> | ..."
        line = [l for l in out.splitlines() if "[Queued]" in l][0]
        transfer_id_prefix = line.split("|")[0].replace("[Queued]", "").strip()

        # Query database directly to get full ID
        from homevault_protocol.database import TransferDatabase
        from homevault_sender.queue import SenderQueue

        db = TransferDatabase(custom_config[1])
        q = SenderQueue(db)
        job = q.get_all_jobs()[0]
        db.close()

        code = main(["--config", str(config_file), "cancel", job.transfer_id])
        assert code == 0
        captured = capsys.readouterr()
        assert "successfully cancelled" in captured.out

    def test_retry_command(self, custom_config, tmp_path, capsys):
        config_file, db_file = custom_config
        from homevault_protocol.database import TransferDatabase
        from homevault_sender.queue import SenderQueue

        db = TransferDatabase(db_file)
        q = SenderQueue(db)
        f = tmp_path / "test_retry.txt"
        f.write_text("Retry test")
        job = q.enqueue_file(f)
        job.state = TransferState.FAILED
        job.error_message = "Network down"
        db.update(job)
        db.close()

        code = main(["--config", str(config_file), "retry", job.transfer_id])
        assert code == 0
        captured = capsys.readouterr()
        assert "reset to PENDING_UPLOAD" in captured.out

    def test_finder_special_character_and_space_paths(self, custom_config, tmp_path, capsys):
        config_file, db_file = custom_config
        # Finder passes paths with spaces, unicode accents, and symbols
        tricky_dir = tmp_path / "My Documents (2026)"
        tricky_dir.mkdir()
        tricky_file = tricky_dir / "résumé & notes [v1].pdf"
        tricky_file.write_bytes(b"%PDF-1.4 test content")

        code = main(["--config", str(config_file), "send", "--mode", "copy", str(tricky_file)])
        assert code == 0
        captured = capsys.readouterr()
        assert "[Queued]" in captured.out
        assert "résumé & notes [v1].pdf" in captured.out

    def test_send_directory_rejected(self, custom_config, tmp_path, capsys):
        config_file, _ = custom_config
        folder = tmp_path / "a_directory"
        folder.mkdir()

        code = main(["--config", str(config_file), "send", str(folder)])
        assert code == 1
        captured = capsys.readouterr()
        assert "directories are not supported" in captured.err

    def test_cli_invalid_mode(self, custom_config, tmp_path, capsys):
        config_file, _ = custom_config
        sample = tmp_path / "test.txt"
        sample.write_text("Hello")

        with pytest.raises(SystemExit) as exc_info:
            main(["--config", str(config_file), "send", "--mode", "invalid_mode", str(sample)])
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err
