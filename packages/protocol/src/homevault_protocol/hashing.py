"""Streaming SHA-256 file hashing.

Hashes files in 8 KB chunks to avoid loading large files entirely into RAM.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Chunk size for streaming hash computation (8 KB).
_HASH_CHUNK_SIZE = 8 * 1024


def hash_file(path: str | Path) -> str:
    """Compute the SHA-256 hex digest of *path*.

    Reads the file in 8 KB chunks so arbitrarily large files can be hashed
    without excessive memory use.

    Args:
        path: Filesystem path to the file.

    Returns:
        Lowercase hex-encoded SHA-256 digest (64 characters).

    Raises:
        FileNotFoundError: If *path* does not exist.
        IsADirectoryError: If *path* is a directory.
        PermissionError:   If the file cannot be read.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    """Compute the SHA-256 hex digest of an in-memory byte string.

    Useful for verifying small payloads (metadata JSON, receipts).
    """
    return hashlib.sha256(data).hexdigest()


def verify_file(path: str | Path, expected_hash: str) -> bool:
    """Return True if the file at *path* matches *expected_hash*.

    Comparison is case-insensitive to tolerate mixed-case hex strings.
    """
    return hash_file(path).lower() == expected_hash.lower()
