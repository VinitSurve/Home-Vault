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
