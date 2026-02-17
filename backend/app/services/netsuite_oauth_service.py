"""NetSuite OAuth 2.0 PKCE flow service."""

from __future__ import annotations

import base64
import hashlib
import os
import time
import urllib.parse

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import decrypt_credentials, encrypt_credentials

logger = structlog.get_logger()

AUTHORIZE_URL = "https://system.netsuite.com/app/login/oauth2/authorize.nl"


def _token_url(account_id: str) -> str:
    slug = account_id.replace("_", "-").lower()
    return f"https://{slug}.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token"


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier_bytes = os.urandom(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url(account_id: str, state: str, code_challenge: str) -> str:
    """Construct the NetSuite OAuth 2.0 authorize URL."""
    params = {
        "response_type": "code",
        "client_id": settings.NETSUITE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.NETSUITE_OAUTH_REDIRECT_URI,
        "scope": settings.NETSUITE_OAUTH_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(account_id: str, code: str, code_verifier: str) -> dict:
    """Exchange an authorization code for tokens."""
    url = _token_url(account_id)
    form_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.NETSUITE_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
        "client_id": settings.NETSUITE_OAUTH_CLIENT_ID,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=form_data, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def refresh_tokens(account_id: str, refresh_token: str) -> dict:
    """Refresh an expired access token."""
    url = _token_url(account_id)
    form_data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.NETSUITE_OAUTH_CLIENT_ID,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=form_data, headers=headers)
        resp.raise_for_status()
        return resp.json()


def build_mcp_authorize_url(
    account_id: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str = "",
) -> str:
    """Construct the NetSuite OAuth 2.0 authorize URL for MCP connectors.

    Uses caller-provided client_id and redirect_uri instead of global settings,
    allowing per-connector OAuth configuration.
    """
    # Default to the same scope configured for regular NetSuite OAuth
    if not scope:
        scope = settings.NETSUITE_OAUTH_SCOPE
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_with_client(
    account_id: str,
    code: str,
    code_verifier: str,
    client_id: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for tokens using a specific client_id and redirect_uri."""
    url = _token_url(account_id)
    form_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "client_id": client_id,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=form_data, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def refresh_tokens_with_client(account_id: str, refresh_token: str, client_id: str) -> dict:
    """Refresh an expired access token using a specific client_id."""
    url = _token_url(account_id)
    form_data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=form_data, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_valid_token(db: AsyncSession, connection) -> str | None:
    """Get a valid access token, auto-refreshing if expired.

    Updates the connection's encrypted_credentials in-place if a refresh occurs.
    Caller is responsible for committing the transaction.
    """
    credentials = decrypt_credentials(connection.encrypted_credentials)

    # OAuth 2.0 credentials have 'access_token'; OAuth 1.0 have 'consumer_key'
    access_token = credentials.get("access_token")
    if not access_token:
        return None

    expires_at = credentials.get("expires_at", 0)
    # Refresh if token expires within 60 seconds (matches reference app)
    if time.time() < (expires_at - 60):
        return access_token

    # Need to refresh
    refresh_token = credentials.get("refresh_token")
    account_id = credentials.get("account_id")
    if not refresh_token or not account_id:
        logger.warning("netsuite.oauth2.missing_refresh_info", connection_id=str(connection.id))
        return None

    try:
        token_data = await refresh_tokens(account_id, refresh_token)
        credentials["access_token"] = token_data["access_token"]
        credentials["refresh_token"] = token_data.get("refresh_token", refresh_token)
        credentials["expires_at"] = time.time() + int(token_data.get("expires_in", 3600))

        connection.encrypted_credentials = encrypt_credentials(credentials)
        await db.flush()

        logger.info("netsuite.oauth2.token_refreshed", connection_id=str(connection.id))
        return credentials["access_token"]
    except Exception:
        logger.exception("netsuite.oauth2.refresh_failed", connection_id=str(connection.id))
        return None
