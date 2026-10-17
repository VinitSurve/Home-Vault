"""macOS OAuth CLI helper for HomeVault.

Interactive authorization tool that completes the Google OAuth 2.0 flow
and securely stores refresh tokens in the macOS Keychain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from homevault_protocol.drive.auth import (
    DEFAULT_KEYRING_SERVICE,
    DEFAULT_KEYRING_USER,
    OAuthManager,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize HomeVault on macOS via Google OAuth 2.0")
    parser.add_argument(
        "--credentials",
        "-c",
        type=Path,
        default=Path.home() / ".homevault" / "credentials.json",
        help="Path to credentials.json downloaded from Google Cloud Console",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check existing authentication status without re-authorizing",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Remove stored OAuth token from macOS Keychain",
    )
    args = parser.parse_args()

    manager = OAuthManager(client_secrets_path=args.credentials)

    if args.logout:
        success = manager.logout()
        if success:
            print("Successfully removed credentials from macOS Keychain.")
            return 0
        else:
            print("No credentials found in macOS Keychain to remove.")
            return 1

    if args.status:
        if manager.is_authenticated():
            print("Status: Authenticated (credentials available in macOS Keychain).")
            return 0
        else:
            print("Status: Not authenticated.")
            return 1

    print(f"Loading OAuth client secrets from: {args.credentials}")
    if not args.credentials.exists():
        print(
            f"Error: Credentials file not found at {args.credentials}.\n"
            "Please create a Desktop OAuth Client in Google Cloud Console, download the JSON,\n"
            f"and place it at {args.credentials}.",
            file=sys.stderr,
        )
        return 1

    try:
        print("Launching browser for Google OAuth 2.0 authorization...")
        creds = manager.login_interactive()
        print("Authorization successful!")
        print(f"Token securely saved to macOS Keychain (Service: {DEFAULT_KEYRING_SERVICE}).")
        return 0
    except Exception as err:
        print(f"Authorization failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
