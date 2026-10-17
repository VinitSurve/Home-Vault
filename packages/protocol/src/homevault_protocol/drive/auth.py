"""OAuth 2.0 authorization and secure token storage for Google Drive API.

Tokens are stored strictly in operating-system secure storage (macOS Keychain
and Windows Credential Manager) using Python's keyring library.
No OAuth tokens or secrets are ever persisted to disk in plaintext.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import keyring
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Narrowest practical scope: app can only see files it created or opened.
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

DEFAULT_KEYRING_SERVICE = "HomeVault-OAuth"
DEFAULT_KEYRING_USER = "google-drive-token"


class AuthenticationRequiredError(Exception):
    """Raised when no valid credentials exist and interactive login is disabled."""


class TokenStorage:
    """Secure credential storage backed by OS Keychain / Credential Manager."""

    def __init__(
        self,
        service: str = DEFAULT_KEYRING_SERVICE,
        username: str = DEFAULT_KEYRING_USER,
    ) -> None:
        self.service = service
        self.username = username

    def save_credentials(self, credentials: Credentials) -> None:
        """Serialize and store OAuth credentials into OS keyring."""
        data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes,
        }
        keyring.set_password(self.service, self.username, json.dumps(data))

    def load_credentials(self) -> Optional[Credentials]:
        """Load OAuth credentials from OS keyring, or None if absent."""
        raw = keyring.get_password(self.service, self.username)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Credentials.from_authorized_user_info(data)
        except Exception:
            return None

    def delete_credentials(self) -> bool:
        """Remove stored credentials from OS keyring."""
        try:
            keyring.delete_password(self.service, self.username)
            return True
        except Exception:
            return False


class OAuthManager:
    """Manages the OAuth 2.0 authorization lifecycle and refresh tokens."""

    def __init__(
        self,
        client_secrets_path: str | Path,
        storage: Optional[TokenStorage] = None,
        scopes: Optional[list[str]] = None,
    ) -> None:
        self.client_secrets_path = Path(client_secrets_path).expanduser()
        self.storage = storage or TokenStorage()
        self.scopes = scopes or [DRIVE_FILE_SCOPE]

    def get_credentials(self, interactive: bool = False) -> Credentials:
        """Retrieve valid credentials, refreshing or prompting if needed.

        Args:
            interactive: If True, launch a browser flow when tokens are missing.

        Returns:
            Authorized google.oauth2.credentials.Credentials instance.

        Raises:
            FileNotFoundError: If client_secrets_path does not exist when needed.
            AuthenticationRequiredError: If not logged in and interactive is False.
        """
        creds = self.storage.load_credentials()

        if creds:
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self.storage.save_credentials(creds)
                    return creds
                except Exception:
                    # Token refresh failed (revoked or expired)
                    creds = None

        if not interactive:
            raise AuthenticationRequiredError(
                "No valid OAuth credentials found. Please run the interactive login first."
            )

        return self.login_interactive()

    def login_interactive(self) -> Credentials:
        """Run the browser-based OAuth 2.0 flow and save token to keyring."""
        if not self.client_secrets_path.exists():
            raise FileNotFoundError(
                f"OAuth client configuration not found at {self.client_secrets_path}. "
                "Please download credentials.json from Google Cloud Console."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secrets_path),
            scopes=self.scopes,
        )
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            access_type="offline",
        )
        self.storage.save_credentials(creds)
        return creds

    def is_authenticated(self) -> bool:
        """Check if stored credentials exist and are valid (or refreshable)."""
        creds = self.storage.load_credentials()
        if not creds:
            return False
        if creds.valid:
            return True
        if creds.refresh_token:
            return True
        return False

    def logout(self) -> bool:
        """Delete stored credentials from OS secure storage."""
        return self.storage.delete_credentials()
