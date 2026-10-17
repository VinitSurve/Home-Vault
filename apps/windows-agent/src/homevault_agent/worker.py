"""Windows receiver background worker service.

Polls Google Drive for pending transfers at configurable intervals,
downloads, decrypts, validates integrity, and commits files to the Inbox.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import keyring

from homevault_agent.discovery import CloudDiscoveryManager
from homevault_protocol.config import ReceiverConfig
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.auth import OAuthManager
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.metadata import CompletionReceipt

logger = logging.getLogger("homevault.receiver")


class ReceiverWorker:
    """Continuous or one-shot background worker for the Windows receiver agent."""

    def __init__(
        self,
        config: ReceiverConfig,
        drive_client: DriveClient,
        database: TransferDatabase,
        discovery_manager: Optional[CloudDiscoveryManager] = None,
        encryption_key: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.drive_client = drive_client
        self.database = database
        self.database.migrate()
        self.dry_run = dry_run
        self._explicit_key = encryption_key
        self._running = False
        self._is_looping = False

        self.discovery_manager = discovery_manager or CloudDiscoveryManager(
            drive_client=self.drive_client,
            inbox_dir=self.config.inbox_path,
            database=self.database,
            cleanup_cloud_after_receipt=self.config.cleanup_delete_cloud_files,
        )

    def get_encryption_key(self) -> str:
        """Retrieve the shared encryption key from secure storage or environment."""
        if self._explicit_key:
            return self._explicit_key

        env_key = os.environ.get("HOMEVAULT_ENCRYPTION_KEY")
        if env_key:
            return env_key.strip()

        stored_key = keyring.get_password("HomeVault", "encryption-key")
        if stored_key:
            return stored_key.strip()

        raise RuntimeError(
            "HomeVault encryption key not found in Windows Credential Manager or environment.\n"
            "Please provision the key securely using:\n"
            '  python -c "import keyring; keyring.set_password(\'HomeVault\', \'encryption-key\', \'<KEY>\')"'
        )

    def poll_and_process(self) -> list[tuple[Optional[Path], Optional[CompletionReceipt]]]:
        """Poll Google Drive once and process any pending transfers."""
        logger.info("Polling Google Drive for pending transfers...")
        pending = self.discovery_manager.discover_pending_transfers()

        if not pending:
            logger.debug("No pending transfers found.")
            return []

        logger.info("Discovered %d pending transfer(s). Processing...", len(pending))
        key = self.get_encryption_key()
        results: list[tuple[Optional[Path], Optional[CompletionReceipt]]] = []

        for meta in pending:
            if not self._running and self._is_looping:
                logger.info("Shutdown requested. Pausing processing.")
                break

            logger.info(
                "Processing transfer %s (file: %s, size: %d bytes)",
                meta.transfer_id,
                meta.filename,
                meta.file_size,
            )
            try:
                path, receipt = self.discovery_manager.download_and_verify(
                    metadata=meta,
                    encryption_key=key,
                    dry_run=self.dry_run,
                )
                if not self.dry_run and path and receipt:
                    logger.info("Successfully delivered %s to %s", meta.filename, path)
                results.append((path, receipt))
            except Exception as e:
                logger.error("Failed to deliver transfer %s: %s", meta.transfer_id, e)

        return results

    def run_once(self) -> list[tuple[Optional[Path], Optional[CompletionReceipt]]]:
        """Execute a single polling iteration and return."""
        self._is_looping = False
        return self.poll_and_process()

    def run_forever(self) -> None:
        """Start the continuous background polling loop with signal handling."""
        self._running = True
        self._is_looping = True
        self._install_signal_handlers()

        poll_interval = max(5, int(self.config.transfer.poll_interval_seconds))
        logger.info(
            "Starting HomeVault Windows Agent (Inbox: %s, Poll interval: %ds, Dry run: %s)",
            self.config.inbox_path,
            poll_interval,
            self.dry_run,
        )

        try:
            while self._running:
                try:
                    self.poll_and_process()
                except Exception as e:
                    logger.error("Unexpected error during polling cycle: %s", e)

                # Sleep in 1-second slices so shutdown signals respond immediately
                for _ in range(poll_interval):
                    if not self._running:
                        break
                    time.sleep(1)

        finally:
            logger.info("HomeVault Windows Agent stopped cleanly.")

    def stop(self) -> None:
        """Signal the worker loop to stop gracefully."""
        self._running = False

    def _install_signal_handlers(self) -> None:
        """Install graceful shutdown signal handlers."""
        def handle_signal(sig, frame):
            logger.info("Received signal %s. Initiating graceful shutdown...", sig)
            self.stop()

        try:
            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
            if hasattr(signal, "SIGBREAK"):  # Available on Windows
                signal.signal(signal.SIGBREAK, handle_signal)
        except (ValueError, OSError) as e:
            logger.debug("Signal handler registration note: %s", e)
