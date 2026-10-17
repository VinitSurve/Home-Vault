"""Tests for DriveClient: quota checks, resumable uploads, downloads, JSON operations."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from homevault_protocol.drive.client import (
    DriveClient,
    InsufficientStorageError,
    QuotaInfo,
)


class TestQuotaChecks:
    def test_quota_with_sufficient_storage(self):
        service = MagicMock()
        service.about().get().execute.return_value = {
            "storageQuota": {
                "limit": "5000000000",
                "usage": "1000000000",
                "usageInDrive": "800000000",
                "usageInDriveTrash": "200000000",
            }
        }
        client = DriveClient(service=service)
        info = client.check_quota(required_bytes=500_000_000)

        assert info.limit_bytes == 5_000_000_000
        assert info.usage_bytes == 1_000_000_000
        assert info.remaining_bytes == 4_000_000_000
        assert not info.has_unlimited_storage

    def test_quota_unlimited_storage(self):
        service = MagicMock()
        service.about().get().execute.return_value = {
            "storageQuota": {
                "usage": "500000000",
            }
        }
        client = DriveClient(service=service)
        info = client.check_quota(required_bytes=10_000_000_000)

        assert info.limit_bytes is None
        assert info.remaining_bytes is None
        assert info.has_unlimited_storage

    def test_quota_insufficient_raises(self):
        service = MagicMock()
        service.about().get().execute.return_value = {
            "storageQuota": {
                "limit": "1000",
                "usage": "900",
            }
        }
        client = DriveClient(service=service)
        with pytest.raises(InsufficientStorageError):
            client.check_quota(required_bytes=200)


class TestFileOperations:
    def test_find_file_exists(self):
        service = MagicMock()
        service.files().list().execute.return_value = {
            "files": [{"id": "file-123", "name": "test.txt", "size": "100"}]
        }
        client = DriveClient(service=service)
        found = client.find_file("test.txt", parent_id="parent-456")

        assert found is not None
        assert found["id"] == "file-123"

    def test_find_file_not_found(self):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": []}
        client = DriveClient(service=service)
        found = client.find_file("missing.txt")
        assert found is None

    def test_list_files_in_folder_paginated(self):
        service = MagicMock()

        page1 = {
            "files": [{"id": "f1", "name": "a.txt"}, {"id": "f2", "name": "b.txt"}],
            "nextPageToken": "token-page-2",
        }
        page2 = {
            "files": [{"id": "f3", "name": "c.txt"}],
        }

        service.files().list().execute.side_effect = [page1, page2]
        client = DriveClient(service=service)
        files = client.list_files_in_folder("folder-id")

        assert len(files) == 3
        assert [f["id"] for f in files] == ["f1", "f2", "f3"]

    def test_upload_json_data(self):
        service = MagicMock()
        create_mock = MagicMock()
        create_mock.execute.return_value = {"id": "json-file-id"}
        service.files.return_value.create.return_value = create_mock
        client = DriveClient(service=service)

        result_id = client.upload_json_data(
            name="metadata.json",
            parent_id="folder-123",
            json_content='{"key": "value"}',
        )
        assert result_id == "json-file-id"
        service.files.return_value.create.assert_called_once()

    def test_delete_file_existing(self):
        service = MagicMock()
        service.files().delete().execute.return_value = {}
        client = DriveClient(service=service)
        assert client.delete_file("file-id") is True

    def test_delete_file_404(self):
        service = MagicMock()
        resp = Response({"status": 404})
        service.files().delete().execute.side_effect = HttpError(resp, b"Not Found")
        client = DriveClient(service=service)
        assert client.delete_file("already-deleted") is False


class TestResumableUploadAndDownload:
    def test_upload_file_resumable_success(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"A" * 1024)

        service = MagicMock()
        request_mock = MagicMock()

        # Simulate 2 chunks: chunk 1 progress, chunk 2 completion
        status1 = MagicMock(resumable_progress=512)
        request_mock.next_chunk.side_effect = [
            (status1, None),
            (None, {"id": "uploaded-file-id"}),
        ]
        service.files().create.return_value = request_mock

        client = DriveClient(service=service, chunk_size=512)

        progress_records = []
        file_id = client.upload_file_resumable(
            local_path=f,
            remote_name="remote.bin",
            parent_id="folder-1",
            progress_callback=lambda cur, tot: progress_records.append((cur, tot)),
        )

        assert file_id == "uploaded-file-id"
        assert len(progress_records) >= 1
        assert progress_records[-1] == (1024, 1024)

    def test_upload_file_resumable_retry_on_503(self, tmp_path):
        f = tmp_path / "retry.bin"
        f.write_bytes(b"Retry test data")

        service = MagicMock()
        request_mock = MagicMock()

        resp_503 = Response({"status": 503})
        err_503 = HttpError(resp_503, b"Service Unavailable")

        # First attempt throws 503, second attempt succeeds
        request_mock.next_chunk.side_effect = [
            err_503,
            (None, {"id": "resumed-file-id"}),
        ]
        service.files().create.return_value = request_mock

        client = DriveClient(service=service, max_retries=2, base_retry_delay=0.01)
        file_id = client.upload_file_resumable(
            local_path=f,
            remote_name="retry.bin",
            parent_id="folder-1",
        )
        assert file_id == "resumed-file-id"

    def test_download_file_resumable_success(self, tmp_path):
        dest = tmp_path / "downloaded.txt"
        service = MagicMock()
        request_mock = MagicMock()
        service.files().get_media.return_value = request_mock

        client = DriveClient(service=service)

        with patch("homevault_protocol.drive.client.MediaIoBaseDownload") as mock_downloader_cls:
            mock_downloader = MagicMock()
            status1 = MagicMock(resumable_progress=500, total_size=1000)
            mock_downloader.next_chunk.side_effect = [
                (status1, False),
                (None, True),
            ]
            mock_downloader_cls.return_value = mock_downloader

            progress_records = []
            client.download_file_resumable(
                file_id="dl-id",
                dest_path=dest,
                progress_callback=lambda cur, tot: progress_records.append((cur, tot)),
            )

            assert dest.exists()
            assert len(progress_records) == 1
            assert progress_records[0] == (500, 1000)
