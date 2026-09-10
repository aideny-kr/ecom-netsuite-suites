"""Metabase MCP OAuth with PKCE, actor-bound single-use state and read scopes.

The public callback only relays a code. An authenticated manager in the original
browser session must finish the exchange; bearer tokens never enter the browser.
"""

import asyncio
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.encryption import decrypt_credentials, encrypt_credentials, get_current_key_version
from app.services.netsuite_oauth_service import generate_pkce_pair
from app.services.public_http import PublicHTTPTransport, validate_endpoint

READ_SCOPES = (
    "agent:resource:read",
    "agent:search",
    "agent:query",
    "agent:query:construct",
    "agent:query:execute",
    "agent:question:execute",
)


class OAuthError(ValueError):
    """Safe operator-facing error; never includes provider response bodies."""


def is_metabase(connector):
    return (
        connector.provider == "custom"
        and connector.auth_type == "oauth2"
        and urlsplit(connector.server_url).path == "/api/metabase-mcp"
    )


def origin(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def callback_uri():
    # Server configuration, never a request Host/X-Forwarded-Host or model input.
    return origin(settings.NETSUITE_OAUTH_REDIRECT_URI) + "/api/v1/mcp-connectors/metabase/oauth/callback"


def same_origin_endpoint(value, resource):
    try:
        endpoint = validate_endpoint(value)
        if origin(endpoint) != origin(resource):
            raise ValueError()
        return endpoint
    except (ValueError, TypeError):
        raise OAuthError("Metabase advertised an unsupported OAuth endpoint.") from None


def state_store():
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)


def state_key(state):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
        raise OAuthError("Sign-in expired or is invalid. Connect with Metabase again.")
    return f"metabase_mcp_oauth:{state}"


async def load_state(state):
    key = state_key(state)
    r = state_store()
    try:
        raw = await r.get(key)
    finally:
        await r.aclose()
    if not raw:
        raise OAuthError("Sign-in expired or was already used. Connect with Metabase again.")
    return decrypt_credentials(raw)


def fingerprint(connector):
    return hashlib.sha256((connector.encrypted_credentials or "").encode()).hexdigest()


async def request_json(url, *, method="GET", **kwargs):
    """No redirects/proxies/private DNS; bounded body and total request duration."""
    try:
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(
                transport=PublicHTTPTransport(url), follow_redirects=False, trust_env=False, timeout=15
            ) as client:
                async with client.stream(method, url, headers={"Accept": "application/json"}, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise OAuthError("Metabase could not complete sign-in. Try connecting again.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise OAuthError("Metabase returned an oversized OAuth response.")
                    import json

                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError()
                    return result
    except OAuthError:
        raise
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise OAuthError("Could not reach Metabase securely. Try connecting again.") from None


async def start(connector, user, app_origin):
    if app_origin not in [s.strip().rstrip("/") for s in settings.CORS_ORIGINS.split(",")]:
        raise OAuthError("Start sign-in from the application Connections page.")
    resource = validate_endpoint(connector.server_url)
    issuer = origin(resource)
    protected = await request_json(issuer + "/.well-known/oauth-protected-resource/api/metabase-mcp")
    if protected.get("resource") != resource or protected.get("authorization_servers") != [issuer]:
        raise OAuthError("Metabase OAuth discovery did not match this connection.")
    metadata = await request_json(issuer + "/.well-known/oauth-authorization-server")
    if (
        metadata.get("issuer") != issuer
        or "S256" not in metadata.get("code_challenge_methods_supported", [])
        or "none" not in metadata.get("token_endpoint_auth_methods_supported", [])
        or not set(READ_SCOPES).issubset(metadata.get("scopes_supported", []))
    ):
        raise OAuthError("Metabase does not advertise the required PKCE and read permissions.")
    authorize = same_origin_endpoint(metadata.get("authorization_endpoint"), resource)
    token = same_origin_endpoint(metadata.get("token_endpoint"), resource)
    registration = same_origin_endpoint(metadata.get("registration_endpoint"), resource)
    redirect = callback_uri()
    credentials = decrypt_credentials(connector.encrypted_credentials) if connector.encrypted_credentials else {}
    if (
        credentials.get("oauth_provider") == "metabase"
        and credentials.get("resource") == resource
        and credentials.get("redirect_uri") == redirect
        and credentials.get("token_endpoint") == token
    ):
        client_id = credentials.get("client_id")
    else:
        client_id = None
    if not client_id:
        registered = await request_json(
            registration,
            method="POST",
            json={
                "client_name": "SuiteStudio",
                "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": " ".join(READ_SCOPES),
            },
        )
        client_id = registered.get("client_id")
        if (
            not isinstance(client_id, str)
            or not client_id
            or len(client_id) > 2048
            or registered.get("token_endpoint_auth_method", "none") != "none"
        ):
            raise OAuthError("Metabase could not register this application for sign-in.")
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    pending = {
        "user_id": str(user.id),
        "tenant_id": str(user.tenant_id),
        "connector_id": str(connector.id),
        "app_origin": app_origin,
        "resource": resource,
        "client_id": client_id,
        "redirect_uri": redirect,
        "token_endpoint": token,
        "code_verifier": verifier,
        "fingerprint": fingerprint(connector),
    }
    r = state_store()
    try:
        await r.setex(state_key(state), 600, encrypt_credentials(pending))
    finally:
        await r.aclose()
    return {
        "state": state,
        "authorize_url": authorize
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
                "scope": " ".join(READ_SCOPES),
            }
        ),
    }


def token_credentials(data, base):
    access = data.get("access_token")
    scope = data.get("scope", base.get("scope", " ".join(READ_SCOPES)))
    if (
        not isinstance(access, str)
        or not access
        or len(access) > 16384
        or data.get("token_type", "").lower() != "bearer"
        or not isinstance(scope, str)
        or not set(scope.split()).issubset(READ_SCOPES)
    ):
        raise OAuthError("Metabase returned an unsupported token or permission grant.")
    try:
        expires_in = int(data.get("expires_in", 3600))
        if not 60 < expires_in <= 366 * 86400:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise OAuthError("Metabase returned an invalid token expiry.") from None
    refresh = data.get("refresh_token", base.get("refresh_token", ""))
    if not isinstance(refresh, str) or len(refresh) > 16384:
        raise OAuthError("Metabase returned an invalid refresh token.")
    return {
        **base,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + expires_in,
        "scope": scope,
    }


async def complete(db, connector, user, state, code, error):
    pending = await load_state(state)
    if any(
        pending.get(k) != str(v)
        for k, v in (
            ("user_id", user.id),
            ("tenant_id", user.tenant_id),
            ("connector_id", connector.id),
        )
    ):
        raise OAuthError("Sign-in belongs to a different session or connection. Start again.")
    # Serialize completion with credential refresh/deletion. Never resurrect a revoked row.
    await db.refresh(connector, with_for_update=True)
    if (
        not is_metabase(connector)
        or connector.status in ("revoked", "superseded")
        or connector.server_url != pending["resource"]
        or fingerprint(connector) != pending["fingerprint"]
    ):
        raise OAuthError("This connection changed during sign-in. Start again.")
    r = state_store()
    try:
        consumed = await r.getdel(state_key(state))
    finally:
        await r.aclose()
    if not consumed or decrypt_credentials(consumed) != pending:
        raise OAuthError("Sign-in expired or was already used. Start again.")
    if error or not code:
        raise OAuthError("Metabase sign-in was canceled or denied. You can try again.")
    data = await request_json(
        same_origin_endpoint(pending["token_endpoint"], connector.server_url),
        method="POST",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
            "resource": connector.server_url,
        },
    )
    credentials = token_credentials(
        data,
        {
            "oauth_provider": "metabase",
            "client_id": pending["client_id"],
            "resource": connector.server_url,
            "token_endpoint": pending["token_endpoint"],
            "redirect_uri": pending["redirect_uri"],
        },
    )
    connector.encrypted_credentials = encrypt_credentials(credentials)
    connector.encryption_key_version = get_current_key_version()
    connector.is_enabled = False
    connector.metadata_json = {
        **(connector.metadata_json or {}),
        "oauth_provider": "metabase",
        "setup_state": "verification_pending",
    }
    from app.services.mcp_connector_service import test_mcp_connector

    result = await test_mcp_connector(db, connector.id, user.tenant_id)
    return result


async def get_token(connector, db):
    if not connector.encrypted_credentials:
        return None
    credentials = decrypt_credentials(connector.encrypted_credentials)
    if credentials.get("resource") != connector.server_url or credentials.get("oauth_provider") != "metabase":
        return None
    if connector.status in ("revoked", "superseded"):
        return None
    if time.time() < credentials.get("expires_at", 0) - 60:
        return credentials.get("access_token")
    if db is None:
        return None
    # PostgreSQL lock serializes rotating refresh tokens across API and worker processes.
    await db.refresh(connector, with_for_update=True)
    if (
        not is_metabase(connector)
        or connector.status in ("revoked", "superseded")
        or not connector.encrypted_credentials
    ):
        return None
    credentials = decrypt_credentials(connector.encrypted_credentials)
    if credentials.get("resource") != connector.server_url or credentials.get("oauth_provider") != "metabase":
        return None
    if time.time() < credentials.get("expires_at", 0) - 60:
        return credentials.get("access_token")
    if not credentials.get("refresh_token") or not credentials.get("client_id"):
        return None
    try:
        data = await request_json(
            same_origin_endpoint(credentials.get("token_endpoint"), connector.server_url),
            method="POST",
            data={
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
                "client_id": credentials["client_id"],
                "resource": connector.server_url,
            },
        )
        credentials = token_credentials(data, credentials)
    except OAuthError:
        return None
    connector.encrypted_credentials = encrypt_credentials(credentials)
    connector.encryption_key_version = get_current_key_version()
    await db.commit()
    return credentials["access_token"]
