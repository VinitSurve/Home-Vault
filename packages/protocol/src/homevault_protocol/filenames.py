"""Safe filename handling and path-traversal prevention.

All filenames received from the sender are sanitized before writing to disk
on the receiver. This module prevents:

- Path traversal (../../../etc/passwd)
- Null bytes
- Windows reserved device names (CON, PRN, NUL, etc.)
- Excessively long filenames
- Filename collisions (appending (1), (2), etc.)
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

# Maximum filename length (excluding extension).
_MAX_NAME_LENGTH = 200

# Windows reserved device names (case-insensitive).
_WINDOWS_RESERVED = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        "COM0", "COM1", "COM2", "COM3", "COM4",
        "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT0", "LPT1", "LPT2", "LPT3", "LPT4",
        "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
)

# Characters unsafe in filenames on Windows (and generally problematic).
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Sanitize a filename for safe use on both macOS and Windows.

    - Strips path components (keeps only the basename).
    - Removes null bytes and control characters.
    - Replaces unsafe characters with underscores.
    - Handles Windows reserved names by appending an underscore.
    - Truncates overly long names while preserving the extension.
    - Normalizes Unicode to NFC form.
    - Falls back to ``untitled`` if nothing remains.

    Args:
        name: The raw filename (may include path components).

    Returns:
        A safe filename string (basename only, no directory).
    """
    # Normalize Unicode
    name = unicodedata.normalize("NFC", name)

    # Strip any path components — use both POSIX and Windows path parsers
    # to handle "/" and "\\" from either platform.
    name = PurePosixPath(name).name
    name = PureWindowsPath(name).name

    # Remove null bytes explicitly (already caught by regex, but be safe)
    name = name.replace("\x00", "")

    # Replace unsafe characters
    name = _UNSAFE_CHARS.sub("_", name)

    # Strip leading/trailing dots and spaces (Windows does not allow these)
    name = name.strip(". ")

    if not name:
        return "untitled"

    # Split into stem and suffix
    p = PureWindowsPath(name)
    stem = p.stem
    suffix = p.suffix  # includes the dot, e.g. ".txt"

    # Handle Windows reserved names
    if stem.upper() in _WINDOWS_RESERVED:
        stem = stem + "_"

    # Truncate stem if too long
    if len(stem) > _MAX_NAME_LENGTH:
        stem = stem[:_MAX_NAME_LENGTH]

    return stem + suffix


def resolve_destination(
    inbox: str | Path,
    filename: str,
) -> Path:
    """Compute a safe, non-colliding destination path within *inbox*.

    1. Sanitizes the filename.
    2. Resolves the full path and asserts it's inside *inbox* (anti-traversal).
    3. If a file with that name already exists, appends ``(1)``, ``(2)``, etc.

    Args:
        inbox:    The root Inbox directory (must exist).
        filename: The raw original filename from the sender.

    Returns:
        An absolute Path that is safe to write to.

    Raises:
        ValueError: If the resolved path escapes the inbox directory.
    """
    inbox = Path(inbox).resolve()
    safe_name = sanitize_filename(filename)
    candidate = inbox / safe_name

    # Anti-traversal: resolved path must be inside inbox
    try:
        candidate.resolve().relative_to(inbox)
    except ValueError:
        raise ValueError(
            f"Resolved path {candidate} escapes inbox directory {inbox}"
        )

    if not candidate.exists():
        return candidate

    # Deduplicate: append (1), (2), … until we find a free name
    stem = PureWindowsPath(safe_name).stem
    suffix = PureWindowsPath(safe_name).suffix
    counter = 1
    while True:
        deduped = f"{stem} ({counter}){suffix}"
        candidate = inbox / deduped
        if not candidate.exists():
            # Re-check traversal for the deduped name
            try:
                candidate.resolve().relative_to(inbox)
            except ValueError:
                raise ValueError(
                    f"Resolved path {candidate} escapes inbox directory {inbox}"
                )
            return candidate
        counter += 1
        if counter > 10_000:
            raise RuntimeError(
                f"Could not find a free filename for {safe_name!r} "
                f"after 10,000 attempts"
            )
