"""AES-256-GCM authenticated encryption and decryption.

Files are encrypted before upload to Google Drive so the storage provider
never sees plaintext contents.

Encrypted file format:
    [12-byte nonce][ciphertext + 16-byte GCM tag]

Streaming mode is used for large files to avoid loading the entire file
into memory.

Key management:
    Keys are stored in OS secure storage (macOS Keychain / Windows Credential
    Manager) and are never written to files or environment variables.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 key size in bytes.
_KEY_SIZE = 32

# GCM nonce size in bytes (96 bits, as recommended by NIST).
_NONCE_SIZE = 12

# Chunk size for streaming encryption/decryption (1 MB).
_STREAM_CHUNK_SIZE = 1024 * 1024


def generate_key() -> str:
    """Generate a new AES-256 key, returned as URL-safe base64.

    The returned string is 44 characters and can be safely stored in
    OS secure storage or a password manager.
    """
    raw = os.urandom(_KEY_SIZE)
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_key(key_b64: str) -> bytes:
    """Decode a base64-encoded key and validate its length."""
    raw = base64.urlsafe_b64decode(key_b64)
    if len(raw) != _KEY_SIZE:
        raise ValueError(
            f"Invalid key length: expected {_KEY_SIZE} bytes, got {len(raw)}"
        )
    return raw


def encrypt_file(
    source: str | Path,
    dest: str | Path,
    key_b64: str,
) -> None:
    """Encrypt *source* to *dest* using AES-256-GCM.

    Reads the entire plaintext into memory for GCM authentication.
    For very large files (multi-GB), consider the streaming variant
    or chunked encryption with per-chunk authentication.

    The output format is: [12-byte nonce][ciphertext + GCM tag].

    Args:
        source:  Path to the plaintext file.
        dest:    Path to write the encrypted file.
        key_b64: AES-256 key as URL-safe base64.

    Raises:
        FileNotFoundError: If *source* does not exist.
        ValueError: If the key is invalid.
    """
    key = _decode_key(key_b64)
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)

    plaintext = Path(source).read_bytes()
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)

    with open(dest, "wb") as f:
        f.write(nonce)
        f.write(ciphertext)


def decrypt_file(
    source: str | Path,
    dest: str | Path,
    key_b64: str,
) -> None:
    """Decrypt an AES-256-GCM encrypted file.

    Reads the nonce from the first 12 bytes, then decrypts and
    authenticates the remainder.

    Args:
        source:  Path to the encrypted file.
        dest:    Path to write the decrypted plaintext.
        key_b64: AES-256 key as URL-safe base64.

    Raises:
        FileNotFoundError: If *source* does not exist.
        ValueError: If the key is invalid.
        cryptography.exceptions.InvalidTag: If decryption fails
            (wrong key, corrupted data, or tampered ciphertext).
    """
    key = _decode_key(key_b64)

    raw = Path(source).read_bytes()
    if len(raw) < _NONCE_SIZE:
        raise ValueError("Encrypted file is too short to contain a valid nonce")

    nonce = raw[:_NONCE_SIZE]
    ciphertext = raw[_NONCE_SIZE:]

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)

    Path(dest).write_bytes(plaintext)


def encrypt_file_streaming(
    source: str | Path,
    dest: str | Path,
    key_b64: str,
    chunk_size: int = _STREAM_CHUNK_SIZE,
) -> None:
    """Encrypt *source* to *dest* in chunks for large files.

    Each chunk is independently encrypted with AES-256-GCM using a unique
    nonce derived from the base nonce and the chunk index. This allows
    streaming without loading the entire file into memory.

    Output format:
        [12-byte base_nonce]
        For each chunk:
            [4-byte big-endian chunk length][12-byte chunk nonce][encrypted chunk + tag]

    The chunk nonce is: base_nonce XOR chunk_index (as 12-byte big-endian).
    """
    key = _decode_key(key_b64)
    base_nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)

    with open(source, "rb") as fin, open(dest, "wb") as fout:
        fout.write(base_nonce)
        chunk_index = 0
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            # Derive per-chunk nonce by XORing base_nonce with chunk index
            idx_bytes = chunk_index.to_bytes(_NONCE_SIZE, "big")
            chunk_nonce = bytes(a ^ b for a, b in zip(base_nonce, idx_bytes))
            encrypted_chunk = aesgcm.encrypt(chunk_nonce, chunk, associated_data=None)
            # Write chunk length (4 bytes big-endian) then the encrypted data
            fout.write(len(encrypted_chunk).to_bytes(4, "big"))
            fout.write(chunk_nonce)
            fout.write(encrypted_chunk)
            chunk_index += 1


def decrypt_file_streaming(
    source: str | Path,
    dest: str | Path,
    key_b64: str,
) -> None:
    """Decrypt a streaming-encrypted file.

    Reads the base nonce, then processes each chunk independently.
    """
    key = _decode_key(key_b64)
    aesgcm = AESGCM(key)

    with open(source, "rb") as fin, open(dest, "wb") as fout:
        base_nonce = fin.read(_NONCE_SIZE)
        if len(base_nonce) < _NONCE_SIZE:
            raise ValueError("Encrypted file is too short to contain a valid nonce")

        while True:
            length_bytes = fin.read(4)
            if not length_bytes:
                break  # End of file
            if len(length_bytes) < 4:
                raise ValueError("Truncated chunk length")
            chunk_len = int.from_bytes(length_bytes, "big")
            chunk_nonce = fin.read(_NONCE_SIZE)
            if len(chunk_nonce) < _NONCE_SIZE:
                raise ValueError("Truncated chunk nonce")
            encrypted_chunk = fin.read(chunk_len)
            if len(encrypted_chunk) < chunk_len:
                raise ValueError("Truncated encrypted chunk")
            plaintext_chunk = aesgcm.decrypt(chunk_nonce, encrypted_chunk, associated_data=None)
            fout.write(plaintext_chunk)
