"""Google Drive API v3 client with resumable transfers and exponential backoff.

Handles:
- Storage quota validation (prevent uploads when Drive space is exhausted).
- Resumable chunked uploads with progress tracking and retry backoff.
- Resumable chunked downloads with progress tracking.
- JSON upload and retrieval for metadata and completion receipts.
- Safe deletion of staged files.
"""

from __future__ import annotations

import io
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import (
    MediaFileUpload,
    MediaInMemoryUpload,
    MediaIoBaseDownload,
)

from homevault_protocol.drive.folders import DriveFolderMap, FolderManager

logger = logging.getLogger(__name__)

# Default chunk size for upload/download: 10 MB (must be multiple of 256 KB for Drive API)
DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024

# Transient HTTP status codes safe for retries
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class InsufficientStorageError(Exception):
    """Raised when available Google Drive storage is insufficient for the upload."""


@dataclass
class QuotaInfo:
    """Information on Google Drive storage quota."""

    limit_bytes: Optional[int]
    usage_bytes: int
    remaining_bytes: Optional[int]
    usage_in_drive: int
    usage_in_trash: int

    @property
    def has_unlimited_storage(self) -> bool:
        return self.limit_bytes is None or self.limit_bytes == 0


class DriveClient:
    """Robust client wrapper for Google Drive API v3."""

    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        service: Optional[Any] = None,
        folder_manager: Optional[FolderManager] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_retries: int = 5,
        base_retry_delay: float = 1.0,
    ) -> None:
        self.credentials = credentials
        self._service = service
        self.folder_manager = folder_manager or FolderManager()
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.base_retry_delay = base_retry_delay

    @property
    def service(self) -> Any:
        """Lazily initialize Google Drive service resource."""
        if self._service is None:
            if not self.credentials:
                raise ValueError("Credentials required to initialize Drive service.")
            self._service = build("drive", "v3", credentials=self.credentials, cache_discovery=False)
        return self._service

    def check_quota(self, required_bytes: int = 0) -> QuotaInfo:
        """Query storage quota and optionally assert sufficient space.

        Args:
            required_bytes: Number of bytes required for an upcoming upload.

        Returns:
            QuotaInfo object.

        Raises:
            InsufficientStorageError: If required_bytes exceeds available space.
        """
        response = self.service.about().get(fields="storageQuota").execute()
        quota = response.get("storageQuota", {})

        limit = int(quota["limit"]) if "limit" in quota else None
        usage = int(quota.get("usage", 0))
        usage_in_drive = int(quota.get("usageInDrive", 0))
        usage_in_trash = int(quota.get("usageInDriveTrash", 0))

        remaining = (limit - usage) if limit is not None else None

        info = QuotaInfo(
            limit_bytes=limit,
            usage_bytes=usage,
            remaining_bytes=remaining,
            usage_in_drive=usage_in_drive,
            usage_in_trash=usage_in_trash,
        )

        if required_bytes > 0 and remaining is not None and remaining < required_bytes:
            raise InsufficientStorageError(
                f"Insufficient Google Drive storage: requires {required_bytes} bytes, "
                f"but only {remaining} bytes remaining (limit: {limit}, usage: {usage})."
            )

        return info

    def ensure_folders(self) -> DriveFolderMap:
        """Find or create the HomeVault folder hierarchy on Drive."""
        return self.folder_manager.ensure_folders(self.service)

    def find_file(
        self,
        name: str,
        parent_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Search for a file by exact name, optionally constrained by parent folder."""
        query_parts = [
            f"name = '{name}'",
            "trashed = false",
        ]
        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")

        query = " and ".join(query_parts)
        response = self.service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name, size, md5Checksum, createdTime, modifiedTime)",
            pageSize=10,
        ).execute()

        files = response.get("files", [])
        return files[0] if files else None

    def list_files_in_folder(
        self,
        folder_id: str,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """List all non-trashed files directly inside *folder_id*."""
        files: list[dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            response = self.service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces="drive",
                fields="nextPageToken, files(id, name, size, createdTime, modifiedTime)",
                pageSize=page_size,
                pageToken=page_token,
            ).execute()

            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return files

    def upload_file_resumable(
        self,
        local_path: Path,
        remote_name: str,
        parent_id: str,
        chunk_size: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_retries: Optional[int] = None,
    ) -> str:
        """Upload a local file using Google Drive resumable chunked upload.

        Args:
            local_path: Path to the local file to upload.
            remote_name: Remote filename on Google Drive.
            parent_id: Destination Drive folder ID.
            chunk_size: Chunk size in bytes (default 10 MB).
            progress_callback: Callback receiving (bytes_uploaded, total_bytes).
            max_retries: Maximum attempts for transient network errors.

        Returns:
            The uploaded Google Drive file ID.
        """
        local_path = Path(local_path)
        file_size = local_path.stat().st_size
        effective_chunk_size = chunk_size or self.chunk_size
        retries_limit = max_retries if max_retries is not None else self.max_retries

        media = MediaFileUpload(
            str(local_path),
            mimetype="application/octet-stream",
            chunksize=effective_chunk_size,
            resumable=True,
        )

        file_metadata = {
            "name": remote_name,
            "parents": [parent_id],
        }

        request = self.service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
        )

        response = None
        attempt = 0

        while response is None:
            try:
                status, response = request.next_chunk()
                if status and progress_callback:
                    progress_callback(status.resumable_progress, file_size)
                attempt = 0  # reset retry counter on successful chunk
            except HttpError as err:
                if err.resp.status in RETRYABLE_STATUS_CODES and attempt < retries_limit:
                    attempt += 1
                    sleep_time = self.base_retry_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient HTTP error %s during upload chunk. Retrying in %.2fs (attempt %d/%d)...",
                        err.resp.status,
                        sleep_time,
                        attempt,
                        retries_limit,
                    )
                    time.sleep(sleep_time)
                else:
                    raise
            except (ConnectionError, TimeoutError, OSError) as err:
                if attempt < retries_limit:
                    attempt += 1
                    sleep_time = self.base_retry_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Network error %s during upload chunk. Retrying in %.2fs (attempt %d/%d)...",
                        err,
                        sleep_time,
                        attempt,
                        retries_limit,
                    )
                    time.sleep(sleep_time)
                else:
                    raise

        if progress_callback:
            progress_callback(file_size, file_size)

        return response["id"]

    def upload_json_data(
        self,
        name: str,
        parent_id: str,
        json_content: str,
    ) -> str:
        """Upload small JSON payload atomically (e.g. metadata or receipts)."""
        data_bytes = json_content.encode("utf-8")
        media = MediaInMemoryUpload(data_bytes, mimetype="application/json", resumable=False)
        metadata = {
            "name": name,
            "parents": [parent_id],
        }
        result = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
        ).execute()
        return result["id"]

    def download_file_resumable(
        self,
        file_id: str,
        dest_path: Path,
        chunk_size: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        """Download a Drive file in chunks with retries and progress tracking."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        effective_chunk_size = chunk_size or self.chunk_size
        retries_limit = max_retries if max_retries is not None else self.max_retries

        request = self.service.files().get_media(fileId=file_id)

        # Temp file during download to prevent corrupt partial destination
        temp_dest = dest_path.with_suffix(dest_path.suffix + ".part")
        with open(temp_dest, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=effective_chunk_size)
            done = False
            attempt = 0

            while not done:
                try:
                    status, done = downloader.next_chunk()
                    if status and progress_callback:
                        progress_callback(status.resumable_progress, status.total_size)
                    attempt = 0
                except HttpError as err:
                    if err.resp.status in RETRYABLE_STATUS_CODES and attempt < retries_limit:
                        attempt += 1
                        sleep_time = self.base_retry_delay * (2 ** (attempt - 1))
                        logger.warning(
                            "Transient HTTP error %s during download chunk. Retrying in %.2fs...",
                            err.resp.status,
                            sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        temp_dest.unlink(missing_ok=True)
                        raise
                except (ConnectionError, TimeoutError, OSError) as err:
                    if attempt < retries_limit:
                        attempt += 1
                        sleep_time = self.base_retry_delay * (2 ** (attempt - 1))
                        logger.warning(
                            "Network error %s during download chunk. Retrying in %.2fs...",
                            err,
                            sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        temp_dest.unlink(missing_ok=True)
                        raise

        temp_dest.replace(dest_path)

    def read_json_file(self, file_id: str) -> dict[str, Any]:
        """Download and deserialize a JSON document from Drive."""
        request = self.service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        content = fh.getvalue().decode("utf-8")
        return json.loads(content)

    def delete_file(self, file_id: str) -> bool:
        """Permanently delete a file from Drive."""
        try:
            self.service.files().delete(fileId=file_id).execute()
            return True
        except HttpError as err:
            if err.resp.status == 404:
                return False  # Already deleted
            raise
