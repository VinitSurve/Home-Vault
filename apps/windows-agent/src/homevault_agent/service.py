"""Windows receiver agent background service entry point.

Run via:
    python -m homevault_agent.service [--once] [--dry-run] [--inbox D:\HomeVault\Inbox]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from homevault_agent.worker import ReceiverWorker
from homevault_protocol.config import get_receiver_config
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.auth import OAuthManager
from homevault_protocol.drive.client import DriveClient


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging to stderr without exposing secrets."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HomeVault Windows Receiver Agent — background cloud sync",
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

    args = parser.parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("homevault.service")

    # Load configuration
    config = get_receiver_config(args.config)
    if args.inbox:
        config.inbox_path = str(args.inbox)
    if args.interval:
        config.transfer.poll_interval_seconds = float(args.interval)

    # 1. Verify OAuth authentication
    creds_path = Path(config.drive.credentials_file).expanduser()
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

    # 2. Initialize Drive client and SQLite database
    drive_client = DriveClient(credentials=credentials)
    db_path = Path(config.db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database = TransferDatabase(db_path)

    # 3. Create worker
    try:
        worker = ReceiverWorker(
            config=config,
            drive_client=drive_client,
            database=database,
            dry_run=args.dry_run,
        )
    except Exception as e:
        logger.error("Failed to initialize receiver worker: %s", e)
        return 1

    # 4. Run once or continuous loop
    if args.once:
        logger.info("Executing single polling cycle...")
        results = worker.run_once()
        logger.info("Cycle finished. %d transfer(s) processed.", len(results))
        return 0
    else:
        worker.run_forever()
        return 0


if __name__ == "__main__":
    sys.exit(main())
