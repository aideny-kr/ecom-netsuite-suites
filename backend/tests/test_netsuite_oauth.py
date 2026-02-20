"""Tests for NetSuite OAuth 2.0 PKCE service."""

import base64
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.netsuite_oauth_service import (
    build_authorize_url,
    exchange_code,
    generate_pkce_pair,
    get_valid_token,
    refresh_tokens,
)


class TestPKCEPairGeneration:
    def test_verifier_is_base64url(self):
        verifier, challenge = generate_pkce_pair()
        # base64url characters only (no padding)
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in verifier)

    def test_verifier_length(self):
        verifier, _ = generate_pkce_pair()
        # 32 random bytes → 43 base64url chars (no padding)
        assert len(verifier) == 43

    def test_challenge_is_sha256_of_verifier(self):
        verifier, challenge = generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        )
        assert challenge == expected

    def test_pairs_are_unique(self):
        pairs = [generate_pkce_pair() for _ in range(10)]
        verifiers = [p[0] for p in pairs]
        assert len(set(verifiers)) == 10


class TestBuildAuthorizeUrl:
    @patch("app.services.netsuite_oauth_service.settings")
    def test_url_contains_required_params(self, mock_settings):
        mock_settings.NETSUITE_OAUTH_CLIENT_ID = "test-client-id"
        mock_settings.NETSUITE_OAUTH_REDIRECT_URI = "http://localhost:3000/callback"
        mock_settings.NETSUITE_OAUTH_SCOPE = "rest_webservices mcp"

        url = build_authorize_url("12345_SB1", "mystate", "mychallenge")

        assert "system.netsuite.com" in url
        assert "response_type=code" in url
        assert "client_id=test-client-id" in url
        assert "state=mystate" in url
        assert "code_challenge=mychallenge" in url
        assert "code_challenge_method=S256" in url
        assert "redirect_uri=" in url
        assert "scope=" in url


class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_exchange_sends_correct_body(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "at_123",
            "refresh_token": "rt_456",
            "expires_in": 3600,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.netsuite_oauth_service.httpx.AsyncClient") as mock_client:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await exchange_code("12345-sb1", "auth_code", "verifier123")

            assert result["access_token"] == "at_123"
            assert result["refresh_token"] == "rt_456"

            # Verify the POST body
            call_kwargs = client_instance.post.call_args
            data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "auth_code"
            assert data["code_verifier"] == "verifier123"


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_refresh_sends_correct_body(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "at_new",
            "refresh_token": "rt_new",
            "expires_in": 3600,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.netsuite_oauth_service.httpx.AsyncClient") as mock_client:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await refresh_tokens("12345-sb1", "rt_old")

            assert result["access_token"] == "at_new"
            call_kwargs = client_instance.post.call_args
            data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "rt_old"


class TestGetValidToken:
    @pytest.mark.asyncio
    async def test_returns_token_when_not_expired(self):
        connection = MagicMock()
        credentials = {
            "auth_type": "oauth2",
            "access_token": "valid_token",
            "refresh_token": "rt",
            "expires_at": time.time() + 3600,
            "account_id": "12345",
        }

        with patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials):
            db = AsyncMock()
            token = await get_valid_token(db, connection)
            assert token == "valid_token"

    @pytest.mark.asyncio
    async def test_refreshes_when_expired(self):
        connection = MagicMock()
        credentials = {
            "auth_type": "oauth2",
            "access_token": "expired_token",
            "refresh_token": "rt_valid",
            "expires_at": time.time() - 100,  # already expired
            "account_id": "12345",
        }

        with (
            patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials),
            patch("app.services.netsuite_oauth_service.refresh_tokens_with_client", new_callable=AsyncMock) as mock_refresh,
            patch("app.services.netsuite_oauth_service.encrypt_credentials", return_value="encrypted"),
        ):
            mock_refresh.return_value = {
                "access_token": "new_token",
                "refresh_token": "new_rt",
                "expires_in": 3600,
            }
            db = AsyncMock()
            token = await get_valid_token(db, connection)

            assert token == "new_token"
            mock_refresh.assert_awaited_once_with("12345", "rt_valid", settings.NETSUITE_OAUTH_CLIENT_ID)

    @pytest.mark.asyncio
    async def test_returns_none_for_oauth1_credentials(self):
        connection = MagicMock()
        credentials = {
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
            "account_id": "12345",
        }

        with patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials):
            db = AsyncMock()
            token = await get_valid_token(db, connection)
            assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_refresh_failure(self):
        connection = MagicMock()
        credentials = {
            "auth_type": "oauth2",
            "access_token": "expired",
            "refresh_token": "rt",
            "expires_at": time.time() - 100,
            "account_id": "12345",
        }

        with (
            patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials),
            patch(
                "app.services.netsuite_oauth_service.refresh_tokens",
                new_callable=AsyncMock,
                side_effect=Exception("Network error"),
            ),
        ):
            db = AsyncMock()
            token = await get_valid_token(db, connection)
            assert token is None
