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

from homevault_protocol.models import Platform, TransferMode, validate_platform


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
    target_platform: Optional[str] = None

    def __post_init__(self) -> None:
        sender = validate_platform(self.sender_platform, "sender_platform")
        self.sender_platform = sender.value

        if self.target_platform is None or not str(self.target_platform).strip():
            target = Platform.WINDOWS if sender == Platform.MACOS else Platform.MACOS
        else:
            target = validate_platform(self.target_platform, "target_platform")

        if sender == target:
            raise ValueError(
                f"Sender platform and target platform cannot be the same: {sender.value!r}"
            )
        self.target_platform = target.value

    def to_json(self) -> str:
        """Serialize to a formatted JSON string."""
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> TransferMetadata:
        """Create TransferMetadata from a dictionary, safely ignoring unexpected keys."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, raw: str | bytes) -> TransferMetadata:
        """Deserialize from a JSON string or bytes."""
        data = json.loads(raw)
        return cls.from_dict(data)

    @classmethod
    def from_job(cls, job) -> TransferMetadata:
        """Create metadata from a TransferJob instance."""
        sender = getattr(job, "sender_platform", Platform.MACOS)
        sender_val = sender.value if hasattr(sender, "value") else str(sender)

        target = getattr(job, "target_platform", None)
        target_val = (
            target.value if hasattr(target, "value") else str(target)
        ) if target else None

        mode = getattr(job, "mode", "copy")
        mode_val = mode.value if hasattr(mode, "value") else str(mode)

        return cls(
            transfer_id=job.transfer_id,
            mode=mode_val,
            filename=job.filename,
            file_size=job.file_size,
            sha256_hash=job.sha256_hash,
            created_at=job.created_at,
            sender_platform=sender_val,
            target_platform=target_val,
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

    def __post_init__(self) -> None:
        receiver = validate_platform(self.receiver_platform, "receiver_platform")
        self.receiver_platform = receiver.value

    def to_json(self) -> str:
        """Serialize to a formatted JSON string."""
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> CompletionReceipt:
        """Create CompletionReceipt from a dictionary, safely ignoring unexpected keys."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, raw: str | bytes) -> CompletionReceipt:
        """Deserialize from a JSON string or bytes."""
        data = json.loads(raw)
        return cls.from_dict(data)

    def matches_metadata(self, metadata: TransferMetadata) -> bool:
        """Verify this receipt is consistent with the original transfer metadata.

        Checks:
        - Transfer IDs match.
        - SHA-256 hashes match (file content is identical).
        - File sizes match.
        - Receiver platform matches metadata target platform.

        Returns:
            True if the receipt is valid for the given metadata.
        """
        receiver = getattr(self, "receiver_platform", None)
        target = getattr(metadata, "target_platform", None)

        platform_matches = True
        if target and receiver:
            platform_matches = (
                str(receiver).strip().lower() == str(target).strip().lower()
            )
        elif target and not receiver:
            # Legacy receipt missing receiver_platform: legitimate legacy receipts
            # were exclusively generated by the Windows receiver.
            platform_matches = (str(target).strip().lower() == Platform.WINDOWS.value)

        return (
            self.transfer_id == metadata.transfer_id
            and self.sha256_hash.lower() == metadata.sha256_hash.lower()
            and self.file_size == metadata.file_size
            and platform_matches
        )
