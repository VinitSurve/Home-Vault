"""Transfer data models.

Defines the core types used by both the macOS sender and the Windows receiver:
- TransferMode: Copy or Move
- TransferState: The lifecycle states a transfer passes through
- TransferJob: The persistent record for a single file transfer
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class TransferMode(str, Enum):
    """Whether the transfer is a Copy (keep original) or Move (delete after verified receipt)."""

    COPY = "copy"
    MOVE = "move"


class TransferState(str, Enum):
    """Lifecycle state of a transfer job.

    State ownership:
        Mac-owned:  pending_upload, uploading, queued
        PC-owned:   downloading, verifying, completed
        Either:     failed, cancelled, needs_attention
    """

    PENDING_UPLOAD = "pending_upload"
    UPLOADING = "uploading"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"


@dataclass
class TransferJob:
    """A single file-transfer job.

    This is the authoritative record on each side. The sender creates it;
    the receiver discovers it from Drive metadata.

    Attributes:
        transfer_id:        UUID v4 unique identifier.
        mode:               Copy or Move.
        state:              Current lifecycle state.
        source_path:        Original file path on the Mac (sender-only).
        filename:           Original filename (basename only, no directory).
        file_size:          Size of the original file in bytes.
        sha256_hash:        SHA-256 hex digest of the original plaintext file.
        created_at:         When the job was created (UTC ISO 8601).
        updated_at:         When the job was last modified (UTC ISO 8601).
        drive_file_id:      Google Drive file ID of the staged encrypted blob.
        drive_metadata_id:  Google Drive file ID of the metadata JSON.
        drive_receipt_id:   Google Drive file ID of the completion receipt.
        dest_path:          Final path on the receiver (set after completion).
        retry_count:        Number of retries attempted so far.
        max_retries:        Maximum retries before marking failed.
        error_message:      Human-readable error description, if any.
        upload_session_uri: Resumable upload URI for interrupted uploads.
    """

    transfer_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    mode: TransferMode = TransferMode.COPY
    state: TransferState = TransferState.PENDING_UPLOAD
    source_path: Optional[str] = None
    filename: str = ""
    file_size: int = 0
    sha256_hash: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    drive_file_id: Optional[str] = None
    drive_metadata_id: Optional[str] = None
    drive_receipt_id: Optional[str] = None
    dest_path: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 5
    error_message: Optional[str] = None
    upload_session_uri: Optional[str] = None

    def touch(self) -> None:
        """Update the `updated_at` timestamp to now (UTC)."""
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Serialize to a plain dict (for JSON / SQLite storage)."""
        return {
            "transfer_id": self.transfer_id,
            "mode": self.mode.value,
            "state": self.state.value,
            "source_path": self.source_path,
            "filename": self.filename,
            "file_size": self.file_size,
            "sha256_hash": self.sha256_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "drive_file_id": self.drive_file_id,
            "drive_metadata_id": self.drive_metadata_id,
            "drive_receipt_id": self.drive_receipt_id,
            "dest_path": self.dest_path,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "error_message": self.error_message,
            "upload_session_uri": self.upload_session_uri,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TransferJob:
        """Deserialize from a plain dict."""
        return cls(
            transfer_id=data["transfer_id"],
            mode=TransferMode(data["mode"]),
            state=TransferState(data["state"]),
            source_path=data.get("source_path"),
            filename=data["filename"],
            file_size=data.get("file_size", 0),
            sha256_hash=data.get("sha256_hash", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            drive_file_id=data.get("drive_file_id"),
            drive_metadata_id=data.get("drive_metadata_id"),
            drive_receipt_id=data.get("drive_receipt_id"),
            dest_path=data.get("dest_path"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 5),
            error_message=data.get("error_message"),
            upload_session_uri=data.get("upload_session_uri"),
        )
