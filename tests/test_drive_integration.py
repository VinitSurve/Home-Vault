"""Integration tests for HomeVault Google Drive staging and discovery.

Requires Google Drive API credentials configured at ~/.homevault/credentials.json
and an authenticated OAuth token in the OS keyring.
If credentials or tokens are not present, the tests are skipped automatically.
"""

from pathlib import Path

import pytest

from homevault_agent.discovery import CloudDiscoveryManager
from homevault_protocol.crypto import generate_key
from homevault_protocol.drive.auth import OAuthManager
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.models import TransferJob, TransferMode
from homevault_sender.staging import StagingManager

CREDS_FILE = Path.home() / ".homevault" / "credentials.json"


@pytest.fixture
def live_drive_client():
    if not CREDS_FILE.exists():
        pytest.skip(f"Live credentials file not found at {CREDS_FILE}. Skipping live integration test.")

    auth_manager = OAuthManager(client_secrets_path=CREDS_FILE)
    if not auth_manager.is_authenticated():
        pytest.skip("OAuth token not present in OS keyring. Run 'python -m homevault_sender.oauth' first.")

    try:
        credentials = auth_manager.get_credentials(interactive=False)
        client = DriveClient(credentials=credentials)
        # Verify connectivity
        client.check_quota()
        return client
    except Exception as e:
        pytest.skip(f"Could not connect to Google Drive API: {e}")


def test_live_stage_and_discover_flow(live_drive_client, tmp_path):
    """End-to-end live round-trip test on real Google Drive."""
    key = generate_key()

    # 1. Mac sender stages a test file
    test_file = tmp_path / "mac_test_upload.txt"
    test_file.write_text("Hello from HomeVault Live Integration Test!")

    job = TransferJob(
        mode=TransferMode.COPY,
        source_path=str(test_file),
        filename="mac_test_upload.txt",
    )

    staging_manager = StagingManager(drive_client=live_drive_client, temp_dir=tmp_path / "mac_temp")
    staged_job = staging_manager.stage_job(job, encryption_key=key)

    assert staged_job.drive_file_id is not None
    assert staged_job.drive_metadata_id is not None

    # 2. Windows agent discovers pending job
    inbox = tmp_path / "win_inbox"
    inbox.mkdir()
    discovery_manager = CloudDiscoveryManager(
        drive_client=live_drive_client,
        inbox_dir=inbox,
        temp_dir=tmp_path / "win_temp",
    )

    pending = discovery_manager.discover_pending_transfers()
    matching = [p for p in pending if p.transfer_id == staged_job.transfer_id]
    assert len(matching) == 1
    metadata = matching[0]

    # 3. Windows agent downloads, decrypts, and verifies
    dest_path, receipt = discovery_manager.download_and_verify(metadata, encryption_key=key)
    assert dest_path.exists()
    assert dest_path.read_text() == "Hello from HomeVault Live Integration Test!"
    assert receipt.transfer_id == staged_job.transfer_id

    # 4. Mac sender checks for receipt
    retrieved_receipt = staging_manager.check_and_fetch_receipt(staged_job.transfer_id)
    assert retrieved_receipt is not None
    assert retrieved_receipt.transfer_id == staged_job.transfer_id

    # 5. Cleanup cloud artifacts created during test
    discovery_manager.cleanup_staged_blob(staged_job.transfer_id)
    if staged_job.drive_metadata_id:
        live_drive_client.delete_file(staged_job.drive_metadata_id)
    # Delete receipt file
    folder_map = live_drive_client.ensure_folders()
    receipt_file = live_drive_client.find_file(f"{staged_job.transfer_id}.json", folder_map.receipts_id)
    if receipt_file:
        live_drive_client.delete_file(receipt_file["id"])
