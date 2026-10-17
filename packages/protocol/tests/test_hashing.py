"""Tests for file hashing."""

import tempfile
from pathlib import Path

import pytest

from homevault_protocol.hashing import hash_bytes, hash_file, verify_file


class TestHashFile:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        # SHA-256 of empty input
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert hash_file(f) == expected

    def test_known_content(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_bytes(b"Hello, World!")
        # Pre-computed SHA-256
        expected = "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        assert hash_file(f) == expected

    def test_binary_content(self, tmp_path):
        f = tmp_path / "binary.bin"
        data = bytes(range(256)) * 100
        f.write_bytes(data)
        digest = hash_file(f)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_large_file(self, tmp_path):
        """Test with a file larger than the chunk size (8 KB)."""
        f = tmp_path / "large.bin"
        data = b"x" * (32 * 1024)  # 32 KB
        f.write_bytes(data)
        digest = hash_file(f)
        assert len(digest) == 64

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            hash_file("/nonexistent/path/file.txt")

    def test_directory_raises(self, tmp_path):
        with pytest.raises(IsADirectoryError):
            hash_file(tmp_path)

    def test_deterministic(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"deterministic test content")
        assert hash_file(f) == hash_file(f)


class TestHashBytes:
    def test_empty(self):
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert hash_bytes(b"") == expected

    def test_known(self):
        expected = "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        assert hash_bytes(b"Hello, World!") == expected


class TestVerifyFile:
    def test_matching_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"verify me")
        digest = hash_file(f)
        assert verify_file(f, digest)

    def test_mismatched_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"verify me")
        assert not verify_file(f, "0" * 64)

    def test_case_insensitive(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"case test")
        digest = hash_file(f)
        assert verify_file(f, digest.upper())
