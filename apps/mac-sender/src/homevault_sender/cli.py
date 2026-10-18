"""macOS Command Line Interface for HomeVault.

Provides:
- Instant file queuing for Finder / Apple Shortcuts (`homevault send`, `homevault copy`, `homevault move`).
- Queue status inspection (`homevault status`).
- Job management (`homevault cancel`, `homevault retry`).
- Background worker execution (`homevault worker`).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from homevault_protocol.config import get_sender_config
from homevault_protocol.database import TransferDatabase
from homevault_protocol.drive.auth import OAuthManager
from homevault_protocol.drive.client import DriveClient
from homevault_protocol.models import TransferMode, TransferState
from homevault_sender.notifications import send_notification
from homevault_sender.queue import SenderQueue
from homevault_sender.worker import SenderWorker


def format_size(num_bytes: int) -> str:
    """Format bytes into a human-readable size string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def cmd_send(args: argparse.Namespace, queue: SenderQueue, mode: TransferMode) -> int:
    """Enqueue one or more files for asynchronous transfer."""
    if not args.paths:
        print("Error: No file paths specified to transfer.", file=sys.stderr)
        return 1

    queued_count = 0
    errors = 0

    for path_str in args.paths:
        try:
            job = queue.enqueue_file(path_str, mode=mode)
            print(
                f"[Queued] {job.transfer_id[:8]} | {job.mode.value.upper():4s} | "
                f"{job.filename} ({format_size(job.file_size)})"
            )
            queued_count += 1
        except Exception as e:
            print(f"Error queuing '{path_str}': {e}", file=sys.stderr)
            errors += 1

    if queued_count > 0:
        send_notification(
            title="HomeVault",
            message=f"Queued {queued_count} file(s) for transfer ({mode.value.upper()}).",
        )

    return 0 if errors == 0 else 1


def cmd_status(args: argparse.Namespace, queue: SenderQueue) -> int:
    """Display transfer queue status."""
    jobs = queue.get_all_jobs(limit=args.limit)
    if not jobs:
        print("HomeVault queue is currently empty.")
        return 0

    print("=" * 80)
    print(f"{'ID':8s} {'Mode':5s} {'State':15s} {'Size':10s} {'Filename'}")
    print("=" * 80)
    for j in reversed(jobs):
        state_str = j.state.value
        if j.error_message:
            state_str = f"{state_str} (!)"
        print(f"{j.transfer_id[:8]:8s} {j.mode.value:5s} {state_str:15s} {format_size(j.file_size):10s} {j.filename}")
    print("=" * 80)
    return 0


def cmd_cancel(args: argparse.Namespace, queue: SenderQueue) -> int:
    """Cancel a pending or queued transfer."""
    success = queue.cancel_job(args.transfer_id)
    if success:
        print(f"Transfer {args.transfer_id} successfully cancelled.")
        return 0
    else:
        print(f"Could not cancel transfer {args.transfer_id}. It may be finished or absent.", file=sys.stderr)
        return 1


def cmd_retry(args: argparse.Namespace, queue: SenderQueue) -> int:
    """Retry a failed or needs-attention transfer."""
    success = queue.retry_job(args.transfer_id)
    if success:
        print(f"Transfer {args.transfer_id} reset to PENDING_UPLOAD.")
        return 0
    else:
        print(f"Could not retry transfer {args.transfer_id}. Only FAILED or NEEDS_ATTENTION can be retried.", file=sys.stderr)
        return 1


def cmd_worker(args: argparse.Namespace, config, database: TransferDatabase) -> int:
    """Start the macOS background upload and receipt worker."""
    creds_path = Path(config.drive.credentials_file).expanduser()
    auth_manager = OAuthManager(client_secrets_path=creds_path)

    if not auth_manager.is_authenticated():
        print(
            "Error: Google OAuth credentials not found in macOS Keychain.\n"
            "Please run 'python -m homevault_sender.oauth' first to authorize.",
            file=sys.stderr,
        )
        return 1

    credentials = auth_manager.get_credentials(interactive=False)
    drive_client = DriveClient(credentials=credentials)

    worker = SenderWorker(
        config=config,
        drive_client=drive_client,
        database=database,
    )

    if args.once:
        print("Running single worker iteration...")
        res = worker.run_once()
        print(
            f"Cycle completed: {res['uploaded']} uploaded, "
            f"{res['receipted']} receipted, {res.get('received', 0)} received."
        )
        return 0
    else:
        worker.run_forever()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="homevault",
        description="HomeVault macOS CLI — queue files for transfer to Windows PC",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to config.toml",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. send command
    p_send = subparsers.add_parser("send", help="Enqueue files for transfer")
    p_send.add_argument("paths", nargs="+", help="One or more file paths to transfer")
    p_send.add_argument(
        "--mode",
        "-m",
        choices=["copy", "move"],
        default="copy",
        help="Transfer mode: copy (keep original) or move (delete after verified receipt)",
    )

    # 2. copy command (convenience shortcut)
    p_copy = subparsers.add_parser("copy", help="Enqueue files to Copy (keep originals)")
    p_copy.add_argument("paths", nargs="+", help="Files to copy")

    # 3. move command (convenience shortcut)
    p_move = subparsers.add_parser("move", help="Enqueue files to Move (safe-delete original after receipt)")
    p_move.add_argument("paths", nargs="+", help="Files to move")

    # 4. status command
    p_status = subparsers.add_parser("status", help="Show transfer queue status")
    p_status.add_argument("--limit", "-n", type=int, default=25, help="Number of recent transfers to show")

    # 5. cancel command
    p_cancel = subparsers.add_parser("cancel", help="Cancel a pending transfer")
    p_cancel.add_argument("transfer_id", help="Transfer UUID to cancel")

    # 6. retry command
    p_retry = subparsers.add_parser("retry", help="Retry a failed transfer")
    p_retry.add_argument("transfer_id", help="Transfer UUID to retry")

    # 7. worker command
    p_worker = subparsers.add_parser("worker", help="Run background queue worker daemon")
    p_worker.add_argument("--once", action="store_true", help="Run a single pass and exit")
    p_worker.add_argument("--interval", type=int, default=None, help="Polling interval in seconds")

    args = parser.parse_args(argv)

    config = get_sender_config(args.config)
    if args.command == "worker" and args.interval:
        config.transfer.poll_interval_seconds = float(args.interval)

    db_path = Path(config.db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database = TransferDatabase(db_path)
    queue = SenderQueue(database=database)

    if args.command == "send":
        return cmd_send(args, queue, mode=TransferMode(args.mode))
    elif args.command == "copy":
        return cmd_send(args, queue, mode=TransferMode.COPY)
    elif args.command == "move":
        return cmd_send(args, queue, mode=TransferMode.MOVE)
    elif args.command == "status":
        return cmd_status(args, queue)
    elif args.command == "cancel":
        return cmd_cancel(args, queue)
    elif args.command == "retry":
        return cmd_retry(args, queue)
    elif args.command == "worker":
        return cmd_worker(args, config, database)

    return 0


def cli_entrypoint() -> None:
    """Console script entrypoint."""
    sys.exit(main())


if __name__ == "__main__":
    cli_entrypoint()
