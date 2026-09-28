"""NetSuite OAuth 2.0 PKCE flow service."""

from __future__ import annotations

import base64
import hashlib
import math
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


def build_authorize_url(
    account_id: str,
    state: str,
    code_challenge: str,
    client_id: str = "",
) -> str:
    """Construct the NetSuite OAuth 2.0 authorize URL.

    Uses provided client_id (per-connection) or falls back to global setting.
    """
    resolved_client_id = client_id or settings.NETSUITE_OAUTH_CLIENT_ID
    params = {
        "response_type": "code",
        "client_id": resolved_client_id,
        "redirect_uri": settings.NETSUITE_OAUTH_REDIRECT_URI,
        "scope": settings.NETSUITE_OAUTH_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(
    account_id: str,
    code: str,
    code_verifier: str,
    client_id: str = "",
) -> dict:
    """Exchange an authorization code for tokens.

    Uses provided client_id (per-connection) or falls back to global setting.
    """
    resolved_client_id = client_id or settings.NETSUITE_OAUTH_CLIENT_ID
    url = _token_url(account_id)
    form_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.NETSUITE_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
        "client_id": resolved_client_id,
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
    # Default to MCP scope for MCP connectors
    if not scope:
        scope = settings.NETSUITE_MCP_OAUTH_SCOPE
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


def _usable_token(credentials, min_validity_seconds, rejected_token_sha=None):
    import hashlib

    token = credentials.get("access_token")
    expires = credentials.get("expires_at", 0)
    if not isinstance(token, str) or not token or type(expires) not in (int, float) or not math.isfinite(expires):
        return None
    if time.time() >= expires - min_validity_seconds:
        return None
    if rejected_token_sha and hashlib.sha256(token.encode()).hexdigest() == rejected_token_sha:
        return None
    return token


async def get_valid_token(
    db: AsyncSession, connection, *, min_validity_seconds=60, rejected_token_sha=None
) -> str | None:
    """Return a sufficiently valid token, serializing and committing any rotation.

    A rejected token may be replaced once by the read caller, which reserves the
    extra OAuth/read spend. A different current token wins over another refresh.
    """
    import asyncio

    from app.models.connection import RETIRED_CONNECTION_STATUSES
    from app.services import oauth_refresh_lock

    if type(min_validity_seconds) is not int or not 60 <= min_validity_seconds <= 600:
        raise ValueError("invalid_token_validity_margin")
    credentials = decrypt_credentials(connection.encrypted_credentials)
    identity = (credentials.get("account_id"), credentials.get("client_id"))

    def usable(current):
        if getattr(connection, "status", None) in RETIRED_CONNECTION_STATUSES:
            return None
        if (current.get("account_id"), current.get("client_id")) != identity:
            return None
        return _usable_token(current, min_validity_seconds, rejected_token_sha)

    if token := usable(credentials):
        return token
    if not credentials.get("access_token"):
        return None
    lock_key = f"oauth_refresh:{connection.id}"
    owner = oauth_refresh_lock.acquire(lock_key)
    if owner is None:
        # Another process may still be rotating. Never return its expired token
        # and never race it for the same single-use refresh token.
        for _ in range(5):
            await asyncio.sleep(1)
            await db.refresh(connection)
            credentials = decrypt_credentials(connection.encrypted_credentials)
            if token := usable(credentials):
                return token
        return None
    try:
        await db.refresh(connection)
        credentials = decrypt_credentials(connection.encrypted_credentials)
        if token := usable(credentials):
            return token
        if (
            getattr(connection, "status", None) in RETIRED_CONNECTION_STATUSES
            or (credentials.get("account_id"), credentials.get("client_id")) != identity
        ):
            return None
        refresh_token = credentials.get("refresh_token")
        account_id, client_id = identity
        if not refresh_token or not account_id or not client_id:
            return None
        token_data = await refresh_tokens_with_client(account_id, refresh_token, client_id)
        issued = time.time()
        credentials["access_token"] = token_data["access_token"]
        credentials["refresh_token"] = token_data.get("refresh_token", refresh_token)
        credentials["issued_at"] = issued
        credentials["expires_in"] = int(token_data.get("expires_in", 3600))
        credentials["expires_at"] = issued + credentials["expires_in"]
        connection.encrypted_credentials = encrypt_credentials(credentials)
        # Once the provider has rotated its single-use token, finish persisting
        # the replacement before releasing ownership, even on caller cancellation.
        commit = asyncio.create_task(db.commit())
        try:
            await asyncio.shield(commit)
        except asyncio.CancelledError:
            await commit
            raise
        logger.info("netsuite.oauth2.token_refreshed", connection_id=str(connection.id))
        return usable(credentials)
    except Exception:
        logger.exception("netsuite.oauth2.refresh_failed", connection_id=str(connection.id))
        return None
    finally:
        oauth_refresh_lock.release(lock_key, owner)
