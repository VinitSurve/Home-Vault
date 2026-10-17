"""Google Drive application folder hierarchy manager.

Ensures the required folder structure exists on Google Drive:
HomeVault/
  ├── Staging/
  ├── Metadata/
  └── Receipts/

Caches discovered folder IDs in memory to avoid repetitive API queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


@dataclass
class DriveFolderMap:
    """Cache of Google Drive folder IDs for the HomeVault tree."""

    root_id: str
    staging_id: str
    metadata_id: str
    receipts_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "root_id": self.root_id,
            "staging_id": self.staging_id,
            "metadata_id": self.metadata_id,
            "receipts_id": self.receipts_id,
        }


class FolderManager:
    """Creates and resolves the HomeVault folder hierarchy."""

    def __init__(
        self,
        root_name: str = "HomeVault",
        staging_name: str = "Staging",
        metadata_name: str = "Metadata",
        receipts_name: str = "Receipts",
    ) -> None:
        self.root_name = root_name
        self.staging_name = staging_name
        self.metadata_name = metadata_name
        self.receipts_name = receipts_name
        self._cached_map: Optional[DriveFolderMap] = None

    def get_cached_folders(self) -> Optional[DriveFolderMap]:
        """Return the cached folder map, or None if uninitialized."""
        return self._cached_map

    def clear_cache(self) -> None:
        """Clear cached folder IDs."""
        self._cached_map = None

    def ensure_folders(self, service: Any) -> DriveFolderMap:
        """Find or create all required application folders on Google Drive.

        Args:
            service: An authenticated Google Drive v3 resource object.

        Returns:
            DriveFolderMap containing IDs for root and subfolders.
        """
        if self._cached_map is not None:
            return self._cached_map

        # 1. Resolve or create root folder "HomeVault"
        root_id = self._find_folder(service, name=self.root_name, parent_id=None)
        if not root_id:
            root_id = self._create_folder(service, name=self.root_name, parent_id=None)

        # 2. Resolve or create "Staging"
        staging_id = self._find_folder(service, name=self.staging_name, parent_id=root_id)
        if not staging_id:
            staging_id = self._create_folder(service, name=self.staging_name, parent_id=root_id)

        # 3. Resolve or create "Metadata"
        metadata_id = self._find_folder(service, name=self.metadata_name, parent_id=root_id)
        if not metadata_id:
            metadata_id = self._create_folder(service, name=self.metadata_name, parent_id=root_id)

        # 4. Resolve or create "Receipts"
        receipts_id = self._find_folder(service, name=self.receipts_name, parent_id=root_id)
        if not receipts_id:
            receipts_id = self._create_folder(service, name=self.receipts_name, parent_id=root_id)

        folder_map = DriveFolderMap(
            root_id=root_id,
            staging_id=staging_id,
            metadata_id=metadata_id,
            receipts_id=receipts_id,
        )
        self._cached_map = folder_map
        return folder_map

    def _find_folder(
        self,
        service: Any,
        name: str,
        parent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Search for an existing non-trashed folder with the given name and parent."""
        query_parts = [
            f"mimeType = '{FOLDER_MIME_TYPE}'",
            f"name = '{name}'",
            "trashed = false",
        ]
        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")

        query = " and ".join(query_parts)
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
        ).execute()

        files = response.get("files", [])
        if files:
            return files[0]["id"]
        return None

    def _create_folder(
        self,
        service: Any,
        name: str,
        parent_id: Optional[str] = None,
    ) -> str:
        """Create a new folder and return its Drive file ID."""
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        folder = service.files().create(
            body=metadata,
            fields="id",
        ).execute()
        return folder["id"]
