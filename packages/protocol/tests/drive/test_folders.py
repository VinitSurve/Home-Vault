"""Tests for FolderManager and folder hierarchy creation."""

from unittest.mock import MagicMock

from homevault_protocol.drive.folders import FolderManager


class TestFolderManager:
    def test_ensure_folders_creates_all_when_missing(self):
        manager = FolderManager()
        service = MagicMock()

        # Simulate list queries returning empty (no folders exist initially)
        list_mock = MagicMock()
        list_mock.execute.return_value = {"files": []}
        service.files.return_value.list.return_value = list_mock

        # Simulate folder creations
        created_ids = ["root-id-1", "staging-id-2", "meta-id-3", "receipts-id-4"]
        create_mock = MagicMock()
        create_mock.execute.side_effect = [{"id": cid} for cid in created_ids]
        service.files.return_value.create.return_value = create_mock

        folder_map = manager.ensure_folders(service)

        assert folder_map.root_id == "root-id-1"
        assert folder_map.staging_id == "staging-id-2"
        assert folder_map.metadata_id == "meta-id-3"
        assert folder_map.receipts_id == "receipts-id-4"

        # Verify caching: subsequent calls do not invoke service again
        cached_map = manager.ensure_folders(service)
        assert cached_map == folder_map
        assert service.files.return_value.create.call_count == 4

    def test_ensure_folders_reuses_existing(self):
        manager = FolderManager()
        service = MagicMock()

        # Simulate list queries returning existing folders
        def mock_list_side_effect(*args, **kwargs):
            q = kwargs.get("q", "")
            # Return files based on query
            if "name = 'HomeVault'" in q:
                return {"files": [{"id": "existing-root"}]}
            elif "name = 'Staging'" in q:
                return {"files": [{"id": "existing-staging"}]}
            elif "name = 'Metadata'" in q:
                return {"files": [{"id": "existing-meta"}]}
            elif "name = 'Receipts'" in q:
                return {"files": [{"id": "existing-receipts"}]}
            return {"files": []}

        service.files().list.side_effect = lambda **kwargs: MagicMock(
            execute=lambda: mock_list_side_effect(**kwargs)
        )

        folder_map = manager.ensure_folders(service)

        assert folder_map.root_id == "existing-root"
        assert folder_map.staging_id == "existing-staging"
        assert folder_map.metadata_id == "existing-meta"
        assert folder_map.receipts_id == "existing-receipts"

        # Verify no create calls were made
        service.files().create.assert_not_called()

    def test_clear_cache(self):
        manager = FolderManager()
        service = MagicMock()
        service.files().list().execute.return_value = {"files": [{"id": "f-1"}]}

        manager.ensure_folders(service)
        assert manager.get_cached_folders() is not None

        manager.clear_cache()
        assert manager.get_cached_folders() is None
