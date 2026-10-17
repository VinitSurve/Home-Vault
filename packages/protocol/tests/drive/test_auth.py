"""Tests for Google Drive OAuth token management and keyring storage."""

import json
from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials

from homevault_protocol.drive.auth import (
    AuthenticationRequiredError,
    OAuthManager,
    TokenStorage,
)


class TestTokenStorage:
    def test_save_credentials(self):
        storage = TokenStorage(service="TestService", username="TestUser")
        creds = MagicMock(spec=Credentials)
        creds.token = "access-token-123"
        creds.refresh_token = "refresh-token-456"
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "client-id-789"
        creds.client_secret = "client-secret-abc"
        creds.scopes = ["https://www.googleapis.com/auth/drive.file"]

        with patch("keyring.set_password") as mock_set:
            storage.save_credentials(creds)
            mock_set.assert_called_once()
            args = mock_set.call_args[0]
            assert args[0] == "TestService"
            assert args[1] == "TestUser"
            saved_data = json.loads(args[2])
            assert saved_data["token"] == "access-token-123"
            assert saved_data["refresh_token"] == "refresh-token-456"

    def test_load_credentials_empty(self):
        storage = TokenStorage(service="TestService", username="TestUser")
        with patch("keyring.get_password", return_value=None):
            assert storage.load_credentials() is None

    def test_load_credentials_valid(self):
        storage = TokenStorage(service="TestService", username="TestUser")
        payload = json.dumps({
            "token": "tok",
            "refresh_token": "ref",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "csec",
            "scopes": ["scope"],
        })
        with patch("keyring.get_password", return_value=payload):
            creds = storage.load_credentials()
            assert creds is not None
            assert creds.token == "tok"
            assert creds.refresh_token == "ref"

    def test_delete_credentials(self):
        storage = TokenStorage(service="TestService", username="TestUser")
        with patch("keyring.delete_password") as mock_del:
            assert storage.delete_credentials() is True
            mock_del.assert_called_once_with("TestService", "TestUser")


class TestOAuthManager:
    def test_get_credentials_valid_existing(self, tmp_path):
        creds = MagicMock(spec=Credentials)
        creds.valid = True

        storage = MagicMock(spec=TokenStorage)
        storage.load_credentials.return_value = creds

        manager = OAuthManager(client_secrets_path=tmp_path / "creds.json", storage=storage)
        result = manager.get_credentials(interactive=False)
        assert result == creds

    def test_get_credentials_refresh_expired(self, tmp_path):
        creds = MagicMock(spec=Credentials)
        creds.valid = False
        creds.expired = True
        creds.refresh_token = "valid-refresh-token"

        storage = MagicMock(spec=TokenStorage)
        storage.load_credentials.return_value = creds

        manager = OAuthManager(client_secrets_path=tmp_path / "creds.json", storage=storage)
        with patch.object(creds, "refresh") as mock_refresh:
            result = manager.get_credentials(interactive=False)
            mock_refresh.assert_called_once()
            storage.save_credentials.assert_called_once_with(creds)
            assert result == creds

    def test_get_credentials_raises_when_missing_and_non_interactive(self, tmp_path):
        storage = MagicMock(spec=TokenStorage)
        storage.load_credentials.return_value = None

        manager = OAuthManager(client_secrets_path=tmp_path / "creds.json", storage=storage)
        with pytest.raises(AuthenticationRequiredError):
            manager.get_credentials(interactive=False)

    def test_is_authenticated(self, tmp_path):
        storage = MagicMock(spec=TokenStorage)
        manager = OAuthManager(client_secrets_path=tmp_path / "creds.json", storage=storage)

        storage.load_credentials.return_value = None
        assert not manager.is_authenticated()

        creds = MagicMock(spec=Credentials)
        creds.valid = True
        storage.load_credentials.return_value = creds
        assert manager.is_authenticated()
