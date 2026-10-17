"""Shared configuration model.

Provides typed configuration with defaults for both sender and receiver.
Configuration is loaded from ~/.homevault/config.toml if it exists,
otherwise defaults are used.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Default config directory
_DEFAULT_CONFIG_DIR = Path.home() / ".homevault"


@dataclass
class DriveConfig:
    """Google Drive settings."""

    root_folder_name: str = "HomeVault"
    staging_folder_name: str = "Staging"
    metadata_folder_name: str = "Metadata"
    receipts_folder_name: str = "Receipts"
    credentials_file: str = str(_DEFAULT_CONFIG_DIR / "credentials.json")


@dataclass
class TransferConfig:
    """Transfer behavior settings."""

    max_retries: int = 5
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 300.0
    poll_interval_seconds: float = 60.0
    upload_chunk_size_bytes: int = 10 * 1024 * 1024  # 10 MB
    streaming_encryption_threshold_bytes: int = 100 * 1024 * 1024  # 100 MB


@dataclass
class SenderConfig:
    """macOS sender settings."""

    db_path: str = str(_DEFAULT_CONFIG_DIR / "sender.db")
    drive: DriveConfig = field(default_factory=DriveConfig)
    transfer: TransferConfig = field(default_factory=TransferConfig)


@dataclass
class ReceiverConfig:
    """Windows receiver settings."""

    inbox_path: str = "D:\\HomeVault\\Inbox"
    db_path: str = str(_DEFAULT_CONFIG_DIR / "receiver.db")
    drive: DriveConfig = field(default_factory=DriveConfig)
    transfer: TransferConfig = field(default_factory=TransferConfig)
    cleanup_completed_after_days: int = 7
    cleanup_delete_cloud_files: bool = True


def load_config_file(path: Optional[str | Path] = None) -> dict:
    """Load a TOML configuration file, returning an empty dict if not found."""
    if path is None:
        path = _DEFAULT_CONFIG_DIR / "config.toml"
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)
