r"""Windows unified background service entry point.

Coordinates both:
- Inbound receiving: polls Google Drive for incoming files targeted at Windows,
  downloads, decrypts, and commits them to the local Inbox.
- Outbound sending: polls local queue for outbound files sent to Mac,
  encrypts, stages them to Google Drive, and checks completion receipts.

Run via:
    python -m homevault_agent.service [--once] [--dry-run] [--inbox D:\HomeVault\Inbox]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from homevault_agent.queue import SenderQueue
from homevault_agent.sender_worker import SenderWorker
from homevault_agent.worker import ReceiverWorker
from homevault_protocol.config import get_receiver_config, get_sender_config
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.auth import OAuthManager
from homevault_protocol.drive.client import DriveClient

logger = logging.getLogger("homevault.service")


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging to stderr without exposing secrets."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_unified_cycle(
    receiver_worker: ReceiverWorker,
    sender_worker: Optional[SenderWorker] = None,
) -> dict[str, int]:
    """Execute a single polling pass for both inbound and outbound transfers.

    Ensures that an exception in one direction does not prevent the other direction
    from executing.

    Returns:
        Dict summarizing counts: 'received', 'uploaded', 'receipted'.
    """
    received_count = 0
    uploaded_count = 0
    receipted_count = 0

    # 1. Inbound processing (Mac -> Windows)
    try:
        delivered = receiver_worker.poll_and_process()
        received_count = len(delivered)
    except Exception as err:
        logger.error("Receiver cycle error (inbound transfers): %s", err)

    # 2. Outbound upload processing (Windows -> Mac)
    if sender_worker:
        try:
            uploaded_count = sender_worker.process_pending_uploads()
        except Exception as err:
            logger.error("Sender upload cycle error (outbound transfers): %s", err)

        # 3. Outbound receipt checks
        try:
            receipted_count = sender_worker.check_completion_receipts()
        except Exception as err:
            logger.error("Sender receipt cycle error: %s", err)

    return {
        "received": received_count,
        "uploaded": uploaded_count,
        "receipted": receipted_count,
    }


def run_unified_forever(
    receiver_worker: ReceiverWorker,
    sender_worker: Optional[SenderWorker] = None,
    poll_interval: int = 60,
) -> None:
    """Run continuous unified service loop with graceful shutdown signal handling."""
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        logger.info("Received signal %s. Initiating graceful shutdown...", sig)
        running = False
        receiver_worker.stop()
        if sender_worker:
            sender_worker.stop()

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, OSError) as e:
        logger.debug("Signal handler registration note: %s", e)

    interval = max(5, poll_interval)
    logger.info("Starting HomeVault Windows Unified Service (Poll interval: %ds)", interval)

    try:
        while running:
            try:
                run_unified_cycle(receiver_worker, sender_worker)
            except Exception as err:
                logger.error("Unified cycle iteration error: %s", err)

            for _ in range(interval):
                if not running:
                    break
                time.sleep(1)

    finally:
        logger.info("HomeVault Windows Unified Service stopped cleanly.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="HomeVault Windows Unified Agent — background cloud sync",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to config.toml (default: ~/.homevault/config.toml)",
    )
    parser.add_argument(
        "--inbox",
        "-i",
        type=Path,
        default=None,
        help="Override destination inbox directory (e.g. D:\\HomeVault\\Inbox)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling cycle and exit immediately",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and verify downloads without committing to Inbox or issuing Drive receipts",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override background polling interval in seconds",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging verbosity level",
    )

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    # Load configuration
    rec_config = get_receiver_config(args.config)
    send_config = get_sender_config(args.config)

    if args.inbox:
        rec_config.inbox_path = str(args.inbox)
    if args.interval:
        rec_config.transfer.poll_interval_seconds = float(args.interval)
        send_config.transfer.poll_interval_seconds = float(args.interval)

    # 1. Verify OAuth authentication
    creds_path = Path(rec_config.drive.credentials_file).expanduser()
    auth_manager = OAuthManager(client_secrets_path=creds_path)

    if not auth_manager.is_authenticated():
        logger.error(
            "OAuth credentials not found in Windows Credential Manager.\n"
            "Please run 'python -m homevault_agent.oauth' first to complete authorization."
        )
        return 1

    try:
        credentials = auth_manager.get_credentials(interactive=False)
    except Exception as e:
        logger.error("Failed to load OAuth credentials: %s", e)
        return 1

    # 2. Initialize Drive client and SQLite databases (isolated)
    drive_client = DriveClient(credentials=credentials)

    rec_db_path = Path(rec_config.db_path).expanduser()
    rec_db_path.parent.mkdir(parents=True, exist_ok=True)
    rec_database = TransferDatabase(rec_db_path)

    send_db_path = Path(send_config.db_path).expanduser()
    send_db_path.parent.mkdir(parents=True, exist_ok=True)
    send_database = TransferDatabase(send_db_path)

    # 3. Create workers
    try:
        receiver_worker = ReceiverWorker(
            config=rec_config,
            drive_client=drive_client,
            database=rec_database,
            dry_run=args.dry_run,
        )
        sender_queue = SenderQueue(database=send_database)
        sender_worker = SenderWorker(
            config=send_config,
            drive_client=drive_client,
            database=send_database,
            queue=sender_queue,
        )
    except Exception as e:
        logger.error("Failed to initialize workers: %s", e)
        rec_database.close()
        send_database.close()
        return 1

    # 4. Run once or continuous loop
    try:
        if args.once:
            logger.info("Executing single unified polling cycle...")
            results = run_unified_cycle(receiver_worker, sender_worker)
            logger.info(
                "Cycle finished. Inbound: %d, Uploaded: %d, Receipted: %d",
                results["received"],
                results["uploaded"],
                results["receipted"],
            )
            return 0
        else:
            poll_interval = int(rec_config.transfer.poll_interval_seconds)
            run_unified_forever(receiver_worker, sender_worker, poll_interval=poll_interval)
            return 0
    finally:
        rec_database.close()
        send_database.close()


if __name__ == "__main__":
    sys.exit(main())
