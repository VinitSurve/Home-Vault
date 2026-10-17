"""Transfer metadata and completion receipt schemas.

Metadata files are JSON documents uploaded to Google Drive alongside the
encrypted file. They carry enough information for the receiver to identify,
download, decrypt, and verify the transfer.

Receipt files are JSON documents uploaded by the receiver to confirm
successful delivery. The sender reads receipts to determine when it's safe
to delete the original (for Move operations).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from homevault_protocol.models import TransferMode


@dataclass
class TransferMetadata:
    """Describes a staged transfer on Google Drive.

    Uploaded to HomeVault/Metadata/{transfer_id}.json by the sender.
    """

    transfer_id: str
    mode: str  # "copy" or "move"
    filename: str  # original basename
    file_size: int  # original plaintext size in bytes
    sha256_hash: str  # SHA-256 hex digest of original plaintext
    created_at: str  # ISO 8601 UTC timestamp
    sender_platform: str = "macos"

    def to_json(self) -> str:
        """Serialize to a formatted JSON string."""
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> TransferMetadata:
        """Deserialize from a JSON string."""
        data = json.loads(raw)
        return cls(**data)

    @classmethod
    def from_job(cls, job) -> TransferMetadata:
        """Create metadata from a TransferJob instance."""
        return cls(
            transfer_id=job.transfer_id,
            mode=job.mode.value,
            filename=job.filename,
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            created_at=job.created_at,
        )


@dataclass
class CompletionReceipt:
    """Confirms successful delivery of a transfer.

    Uploaded to HomeVault/Receipts/{transfer_id}.json by the receiver.
    The sender checks for this receipt before deleting the original (Move).
    """

    transfer_id: str
    final_path: str  # where the file was saved on the receiver
    file_size: int  # size of the delivered file
    sha256_hash: str  # verified SHA-256 of the delivered plaintext
    completed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    receiver_platform: str = "windows"

    def to_json(self) -> str:
        """Serialize to a formatted JSON string."""
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> CompletionReceipt:
        """Deserialize from a JSON string."""
        data = json.loads(raw)
        return cls(**data)

    def matches_metadata(self, metadata: TransferMetadata) -> bool:
        """Verify this receipt is consistent with the original transfer metadata.

        Checks:
        - Transfer IDs match.
        - SHA-256 hashes match (file content is identical).
        - File sizes match.

        Returns:
            True if the receipt is valid for the given metadata.
        """
        return (
            self.transfer_id == metadata.transfer_id
            and self.sha256_hash.lower() == metadata.sha256_hash.lower()
            and self.file_size == metadata.file_size
        )
