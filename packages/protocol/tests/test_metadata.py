"""Tests for transfer metadata and completion receipts."""

import json

import pytest

from homevault_protocol.metadata import CompletionReceipt, TransferMetadata
from homevault_protocol.models import TransferJob, TransferMode


class TestTransferMetadata:
    def test_serialization_round_trip(self):
        meta = TransferMetadata(
            transfer_id="test-id-123",
            mode="copy",
            filename="document.pdf",
            file_size=1048576,
            sha256_hash="abc123def456",
            created_at="2024-01-01T00:00:00+00:00",
        )
        json_str = meta.to_json()
        restored = TransferMetadata.from_json(json_str)

        assert restored.transfer_id == "test-id-123"
        assert restored.mode == "copy"
        assert restored.filename == "document.pdf"
        assert restored.file_size == 1048576
        assert restored.sha256_hash == "abc123def456"
        assert restored.sender_platform == "macos"
        assert restored.target_platform == "windows"

    def test_platform_defaults(self):
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=10,
            sha256_hash="abc",
            created_at="2024-01-01T00:00:00Z",
        )
        assert meta.sender_platform == "macos"
        assert meta.target_platform == "windows"

    def test_windows_sender_infers_macos_target(self):
        meta = TransferMetadata(
            transfer_id="id-2",
            mode="copy",
            filename="f.txt",
            file_size=10,
            sha256_hash="abc",
            created_at="2024-01-01T00:00:00Z",
            sender_platform="windows",
        )
        assert meta.sender_platform == "windows"
        assert meta.target_platform == "macos"

    def test_explicit_platforms(self):
        meta = TransferMetadata(
            transfer_id="id-3",
            mode="move",
            filename="f.txt",
            file_size=10,
            sha256_hash="abc",
            created_at="2024-01-01T00:00:00Z",
            sender_platform="windows",
            target_platform="macos",
        )
        assert meta.sender_platform == "windows"
        assert meta.target_platform == "macos"

    def test_same_sender_and_target_raises(self):
        with pytest.raises(ValueError, match="cannot be the same"):
            TransferMetadata(
                transfer_id="id-4",
                mode="copy",
                filename="f.txt",
                file_size=10,
                sha256_hash="abc",
                created_at="2024-01-01T00:00:00Z",
                sender_platform="macos",
                target_platform="macos",
            )

    def test_invalid_platform_raises(self):
        with pytest.raises(ValueError, match="Supported platforms"):
            TransferMetadata(
                transfer_id="id-5",
                mode="copy",
                filename="f.txt",
                file_size=10,
                sha256_hash="abc",
                created_at="2024-01-01T00:00:00Z",
                sender_platform="linux",
            )

    def test_from_job(self):
        job = TransferJob(
            mode=TransferMode.MOVE,
            filename="photo.jpg",
            file_size=2048,
            sha256_hash="hash123",
        )
        meta = TransferMetadata.from_job(job)

        assert meta.transfer_id == job.transfer_id
        assert meta.mode == "move"
        assert meta.filename == "photo.jpg"
        assert meta.file_size == 2048
        assert meta.sha256_hash == "hash123"
        assert meta.sender_platform == "macos"
        assert meta.target_platform == "windows"

    def test_from_job_windows_sender(self):
        from homevault_protocol.models import Platform
        job = TransferJob(
            mode=TransferMode.COPY,
            filename="doc.docx",
            file_size=4096,
            sha256_hash="hash456",
            sender_platform=Platform.WINDOWS,
            target_platform=Platform.MACOS,
        )
        meta = TransferMetadata.from_job(job)
        assert meta.sender_platform == "windows"
        assert meta.target_platform == "macos"

    def test_legacy_json_without_target_platform(self):
        # Simulates metadata produced prior to Phase 5.1
        legacy_json = json.dumps({
            "transfer_id": "legacy-id-1",
            "mode": "copy",
            "filename": "legacy.pdf",
            "file_size": 1024,
            "sha256_hash": "hash_legacy",
            "created_at": "2024-01-01T00:00:00+00:00",
            "sender_platform": "macos",
        })
        meta = TransferMetadata.from_json(legacy_json)
        assert meta.transfer_id == "legacy-id-1"
        assert meta.sender_platform == "macos"
        assert meta.target_platform == "windows"

    def test_legacy_json_without_any_platforms(self):
        # Simulates ancient metadata missing sender_platform entirely
        legacy_json = json.dumps({
            "transfer_id": "legacy-id-2",
            "mode": "move",
            "filename": "ancient.pdf",
            "file_size": 2048,
            "sha256_hash": "hash_ancient",
            "created_at": "2024-01-01T00:00:00+00:00",
        })
        meta = TransferMetadata.from_json(legacy_json)
        assert meta.transfer_id == "legacy-id-2"
        assert meta.sender_platform == "macos"
        assert meta.target_platform == "windows"

    def test_from_json_ignores_unexpected_future_keys(self):
        future_json = json.dumps({
            "transfer_id": "future-id",
            "mode": "copy",
            "filename": "future.pdf",
            "file_size": 100,
            "sha256_hash": "hash_future",
            "created_at": "2026-01-01T00:00:00+00:00",
            "sender_platform": "macos",
            "target_platform": "windows",
            "future_tag": "new_feature_tag",
            "compression_algorithm": "zstd",
        })
        meta = TransferMetadata.from_json(future_json)
        assert meta.transfer_id == "future-id"
        assert meta.filename == "future.pdf"

    def test_json_is_valid(self):
        meta = TransferMetadata(
            transfer_id="id",
            mode="copy",
            filename="f.txt",
            file_size=0,
            sha256_hash="h",
            created_at="t",
        )
        parsed = json.loads(meta.to_json())
        assert isinstance(parsed, dict)
        assert "transfer_id" in parsed
        assert parsed["sender_platform"] == "macos"
        assert parsed["target_platform"] == "windows"


class TestCompletionReceipt:
    def test_serialization_round_trip(self):
        receipt = CompletionReceipt(
            transfer_id="test-id-123",
            final_path="D:\\HomeVault\\Inbox\\document.pdf",
            file_size=1048576,
            sha256_hash="abc123def456",
        )
        json_str = receipt.to_json()
        restored = CompletionReceipt.from_json(json_str)

        assert restored.transfer_id == "test-id-123"
        assert restored.final_path == "D:\\HomeVault\\Inbox\\document.pdf"
        assert restored.file_size == 1048576
        assert restored.receiver_platform == "windows"

    def test_receiver_platform_validation(self):
        r1 = CompletionReceipt(
            transfer_id="id-1",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
            receiver_platform="macos",
        )
        assert r1.receiver_platform == "macos"

        with pytest.raises(ValueError, match="Supported platforms"):
            CompletionReceipt(
                transfer_id="id-2",
                final_path="/inbox/f.txt",
                file_size=100,
                sha256_hash="aabb",
                receiver_platform="linux",
            )

    def test_receipt_from_json_ignores_unexpected_future_keys(self):
        future_json = json.dumps({
            "transfer_id": "id-future",
            "final_path": "/inbox/f.txt",
            "file_size": 100,
            "sha256_hash": "aabb",
            "receiver_platform": "windows",
            "signature": "sig_bytes_here",
        })
        receipt = CompletionReceipt.from_json(future_json)
        assert receipt.transfer_id == "id-future"
        assert receipt.receiver_platform == "windows"

    def test_matches_metadata_valid(self):
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
        )
        receipt = CompletionReceipt(
            transfer_id="id-1",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
        )
        assert receipt.matches_metadata(meta)

    def test_matches_metadata_bidirectional(self):
        # Windows -> Mac transfer
        meta_win_to_mac = TransferMetadata(
            transfer_id="id-wm",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
            sender_platform="windows",
            target_platform="macos",
        )
        receipt_mac = CompletionReceipt(
            transfer_id="id-wm",
            final_path="/Users/user/Inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
            receiver_platform="macos",
        )
        assert receipt_mac.matches_metadata(meta_win_to_mac)

    def test_matches_metadata_platform_mismatch_fails(self):
        # Metadata targets macOS, but receipt claims to be from Windows
        meta = TransferMetadata(
            transfer_id="id-mismatch",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
            sender_platform="windows",
            target_platform="macos",
        )
        receipt_wrong_platform = CompletionReceipt(
            transfer_id="id-mismatch",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
            receiver_platform="windows",
        )
        assert not receipt_wrong_platform.matches_metadata(meta)

    def test_matches_metadata_none_receiver_platform_no_attribute_error(self):
        # Legacy or mock receipt with receiver_platform explicitly None
        receipt = CompletionReceipt(
            transfer_id="id-legacy",
            final_path="C:\\inbox\\f.txt",
            file_size=100,
            sha256_hash="aabb",
        )
        # Manually force receiver_platform to None to simulate mock or legacy object
        receipt.receiver_platform = None

        meta_win = TransferMetadata(
            transfer_id="id-legacy",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
            sender_platform="macos",
            target_platform="windows",
        )
        # Should safely match Windows target (legacy HomeVault 1.0 destination) without AttributeError
        assert receipt.matches_metadata(meta_win)

        meta_mac = TransferMetadata(
            transfer_id="id-legacy",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
            sender_platform="windows",
            target_platform="macos",
        )
        # Must safely reject macOS target without AttributeError
        assert not receipt.matches_metadata(meta_mac)

    def test_matches_metadata_none_target_platform_no_attribute_error(self):
        receipt = CompletionReceipt(
            transfer_id="id-1",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
            receiver_platform="windows",
        )
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
        )
        # Manually force target_platform to None to simulate legacy or mock metadata
        meta.target_platform = None

        # Should match without AttributeError
        assert receipt.matches_metadata(meta)

    def test_matches_metadata_missing_receiver_platform_attr(self):
        # Object without receiver_platform attribute at all
        class BareReceipt:
            transfer_id = "id-bare"
            file_size = 50
            sha256_hash = "beef"

        meta = TransferMetadata(
            transfer_id="id-bare",
            mode="copy",
            filename="f.txt",
            file_size=50,
            sha256_hash="beef",
            created_at="t",
            target_platform="windows",
        )
        bare = BareReceipt()
        # Should not raise AttributeError
        assert CompletionReceipt.matches_metadata(bare, meta)

    def test_matches_metadata_wrong_hash(self):
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
        )
        receipt = CompletionReceipt(
            transfer_id="id-1",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="ccdd",
        )
        assert not receipt.matches_metadata(meta)

    def test_matches_metadata_wrong_size(self):
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
        )
        receipt = CompletionReceipt(
            transfer_id="id-1",
            final_path="/inbox/f.txt",
            file_size=200,
            sha256_hash="aabb",
        )
        assert not receipt.matches_metadata(meta)

    def test_matches_metadata_wrong_id(self):
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="aabb",
            created_at="t",
        )
        receipt = CompletionReceipt(
            transfer_id="id-2",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
        )
        assert not receipt.matches_metadata(meta)

    def test_hash_comparison_case_insensitive(self):
        meta = TransferMetadata(
            transfer_id="id-1",
            mode="copy",
            filename="f.txt",
            file_size=100,
            sha256_hash="AABB",
            created_at="t",
        )
        receipt = CompletionReceipt(
            transfer_id="id-1",
            final_path="/inbox/f.txt",
            file_size=100,
            sha256_hash="aabb",
        )
        assert receipt.matches_metadata(meta)
