"""Tests for AES-256-GCM encryption and decryption."""

import pytest

from homevault_protocol.crypto import (
    decrypt_file,
    decrypt_file_streaming,
    encrypt_file,
    encrypt_file_streaming,
    generate_key,
    _decode_key,
)
from homevault_protocol.hashing import hash_file


class TestKeyGeneration:
    def test_generate_key_length(self):
        key = generate_key()
        assert len(key) == 44  # base64 of 32 bytes

    def test_generate_key_unique(self):
        keys = {generate_key() for _ in range(50)}
        assert len(keys) == 50

    def test_decode_valid_key(self):
        key = generate_key()
        raw = _decode_key(key)
        assert len(raw) == 32

    def test_decode_invalid_key(self):
        with pytest.raises(ValueError, match="Invalid key length"):
            _decode_key("dG9vc2hvcnQ=")  # "tooshort" in base64


class TestEncryptDecrypt:
    def test_round_trip(self, tmp_path):
        key = generate_key()
        original = tmp_path / "original.txt"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.txt"

        original.write_bytes(b"Hello, HomeVault!")
        encrypt_file(original, encrypted, key)
        decrypt_file(encrypted, decrypted, key)

        assert decrypted.read_bytes() == b"Hello, HomeVault!"

    def test_encrypted_differs_from_plaintext(self, tmp_path):
        key = generate_key()
        original = tmp_path / "original.txt"
        encrypted = tmp_path / "encrypted.enc"

        original.write_bytes(b"Secret content")
        encrypt_file(original, encrypted, key)

        assert encrypted.read_bytes() != original.read_bytes()
        assert encrypted.stat().st_size > original.stat().st_size  # nonce + tag overhead

    def test_wrong_key_fails(self, tmp_path):
        key1 = generate_key()
        key2 = generate_key()
        original = tmp_path / "original.txt"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.txt"

        original.write_bytes(b"Secret content")
        encrypt_file(original, encrypted, key1)

        with pytest.raises(Exception):  # InvalidTag
            decrypt_file(encrypted, decrypted, key2)

    def test_tampered_ciphertext_fails(self, tmp_path):
        key = generate_key()
        original = tmp_path / "original.txt"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.txt"

        original.write_bytes(b"Tamper test content")
        encrypt_file(original, encrypted, key)

        # Tamper with the ciphertext (flip a byte after the nonce)
        data = bytearray(encrypted.read_bytes())
        data[15] ^= 0xFF
        encrypted.write_bytes(bytes(data))

        with pytest.raises(Exception):  # InvalidTag
            decrypt_file(encrypted, decrypted, key)

    def test_empty_file(self, tmp_path):
        key = generate_key()
        original = tmp_path / "empty.bin"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.bin"

        original.write_bytes(b"")
        encrypt_file(original, encrypted, key)
        decrypt_file(encrypted, decrypted, key)

        assert decrypted.read_bytes() == b""

    def test_binary_content(self, tmp_path):
        key = generate_key()
        original = tmp_path / "binary.bin"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.bin"

        data = bytes(range(256)) * 100
        original.write_bytes(data)
        encrypt_file(original, encrypted, key)
        decrypt_file(encrypted, decrypted, key)

        assert decrypted.read_bytes() == data

    def test_hash_preserved_through_encryption(self, tmp_path):
        key = generate_key()
        original = tmp_path / "original.txt"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.txt"

        original.write_bytes(b"Hash integrity test")
        original_hash = hash_file(original)

        encrypt_file(original, encrypted, key)
        decrypt_file(encrypted, decrypted, key)

        assert hash_file(decrypted) == original_hash


class TestStreamingEncryption:
    def test_round_trip(self, tmp_path):
        key = generate_key()
        original = tmp_path / "original.bin"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.bin"

        # Create a file larger than the default chunk size
        data = b"streaming test data " * 5000
        original.write_bytes(data)

        encrypt_file_streaming(original, encrypted, key, chunk_size=1024)
        decrypt_file_streaming(encrypted, decrypted, key)

        assert decrypted.read_bytes() == data

    def test_empty_file_streaming(self, tmp_path):
        key = generate_key()
        original = tmp_path / "empty.bin"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.bin"

        original.write_bytes(b"")
        encrypt_file_streaming(original, encrypted, key)
        decrypt_file_streaming(encrypted, decrypted, key)

        assert decrypted.read_bytes() == b""

    def test_hash_preserved_streaming(self, tmp_path):
        key = generate_key()
        original = tmp_path / "original.bin"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.bin"

        data = b"hash test " * 10000
        original.write_bytes(data)
        original_hash = hash_file(original)

        encrypt_file_streaming(original, encrypted, key, chunk_size=4096)
        decrypt_file_streaming(encrypted, decrypted, key)

        assert hash_file(decrypted) == original_hash

    def test_wrong_key_fails_streaming(self, tmp_path):
        key1 = generate_key()
        key2 = generate_key()
        original = tmp_path / "original.bin"
        encrypted = tmp_path / "encrypted.enc"
        decrypted = tmp_path / "decrypted.bin"

        original.write_bytes(b"Secret streaming content")
        encrypt_file_streaming(original, encrypted, key1, chunk_size=512)

        with pytest.raises(Exception):  # InvalidTag
            decrypt_file_streaming(encrypted, decrypted, key2)
