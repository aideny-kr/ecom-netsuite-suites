"""Encrypted Solidus/custom API settings and bounded, read-only verification."""

import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from urllib.parse import unquote, urlsplit

import httpx
from cryptography.fernet import InvalidToken

from app.core.encryption import decrypt_credentials
from app.services.public_http import PublicHTTPTransport, UnsafeEndpointError, validate_endpoint

HTTP_PROVIDERS = frozenset(("solidus", "api"))
_MAX_BYTES = 2 * 1024 * 1024
_BLOCKED_HEADERS = frozenset(
    (
        "host",
        "cookie",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "accept",
        "content-type",
    )
)


class ConnectorReadError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def validate_auth(auth_type, token=None, header_name=None):
    if auth_type not in ("none", "bearer", "api_key"):
        raise ValueError("Choose bearer token, API key, or no authentication")
    if auth_type != "none" and (
        not isinstance(token, str) or not token or len(token) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in token)
    ):
        raise ValueError("Enter a valid credential")
    if auth_type == "api_key":
        if (
            not isinstance(header_name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header_name)
            or header_name.lower() in _BLOCKED_HEADERS
            or header_name.lower().startswith(("proxy-", "sec-"))
        ):
            raise ValueError("Enter a valid authentication header name")


def validate_path(path: str, *, allow_query=False):
    if not isinstance(path, str) or not path or len(path) > 2048:
        raise ValueError("Enter a relative read endpoint")
    decoded = unquote(unquote(path))
    parts = urlsplit(decoded)
    if (
        decoded.startswith("/")
        or parts.scheme
        or parts.netloc
        or parts.fragment
        or (parts.query and not allow_query)
        or "\\" in decoded
        or any(ord(c) < 32 for c in decoded)
        or any(p in (".", "..") for p in parts.path.split("/"))
    ):
        raise ValueError("Enter a relative read endpoint without traversal or URL credentials")
    return path


def validate_credentials(provider: str, credentials: dict) -> dict:
    if not isinstance(credentials, dict):
        raise ValueError("Enter connection settings")
    base = validate_endpoint(credentials.get("base_url", "")).rstrip("/") + "/"
    auth_type = credentials.get("auth_type", "bearer")
    token = credentials.get("token")
    header = credentials.get("header_name") or "X-API-Key"
    validate_auth(auth_type, token, header)
    settings = {"base_url": base, "auth_type": auth_type}
    if auth_type != "none":
        settings["token"] = token
    if auth_type == "api_key":
        settings["header_name"] = header
    if provider == "solidus":
        profile = credentials.get("api_profile", "solidus_rest")
        if profile not in ("solidus_rest", "framework_sync") or auth_type == "none":
            raise ValueError("Choose a Solidus API profile and authentication")
        if profile == "framework_sync" and base != "https://private-direct-access.frame.work/api/":
            raise ValueError("Framework Sync requires the Framework API URL")
        settings["api_profile"] = profile
    else:
        settings["test_path"] = validate_path(credentials.get("test_path", ""))
    return settings


def public_metadata(credentials):
    return {key: credentials[key] for key in ("base_url", "api_profile", "test_path") if key in credentials}


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("invalid_number")


async def read_json(credentials, path, *, client=None):
    validate_path(path, allow_query=True)
    headers = {"Accept": "application/json"}
    auth = credentials["auth_type"]
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {credentials['token']}"
    elif auth == "api_key":
        headers[credentials["header_name"]] = credentials["token"]
    owns_client = client is None
    http = client or httpx.AsyncClient(transport=PublicHTTPTransport(credentials["base_url"]), trust_env=False)
    try:
        async with asyncio.timeout(25):
            async with http.stream(
                "GET",
                credentials["base_url"] + path,
                headers=headers,
                timeout=httpx.Timeout(20, connect=5),
                follow_redirects=False,
            ) as response:
                if response.status_code in (401, 403):
                    raise ConnectorReadError("authentication_failed")
                if response.status_code == 429:
                    raise ConnectorReadError("rate_limited")
                if response.status_code != 200:
                    raise ConnectorReadError("http_error")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > _MAX_BYTES:
                        raise ConnectorReadError("response_too_large")
                    data.extend(chunk)
            result = json.loads(
                data, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_json_pairs
            )
            if not isinstance(result, (dict, list)) or (
                isinstance(result, dict) and (result.get("errors") or result.get("error"))
            ):
                raise ConnectorReadError("invalid_response")
            return result
    except (httpx.HTTPError, TimeoutError, UnsafeEndpointError):
        raise ConnectorReadError("transport_failed") from None
    except (ValueError, UnicodeError, RecursionError, DecimalException):
        raise ConnectorReadError("invalid_response") from None
    finally:
        if owns_client:
            await http.aclose()


async def verify_connection(db, connection):
    try:
        credentials = validate_credentials(connection.provider, decrypt_credentials(connection.encrypted_credentials))
        if connection.provider == "solidus":
            prefix = "sync/" if credentials["api_profile"] == "framework_sync" else ""
            body = await read_json(credentials, f"{prefix}orders?per_page=1&page=1")
            if not isinstance(body, dict) or not isinstance(body.get("orders"), list) or len(body["orders"]) > 1:
                raise ConnectorReadError("unexpected_order_format")
        else:
            await read_json(credentials, credentials["test_path"])
    except (ConnectorReadError, InvalidToken, ValueError, TypeError, AttributeError):
        connection.status = "error"
        connection.error_reason = "Verification failed. Check the URL, credential, and read access."
        result = {"status": "error", "message": connection.error_reason}
    else:
        connection.status = "active"
        connection.error_reason = None
        result = {"status": "ok", "message": "Read access verified"}
    connection.last_health_check_at = datetime.now(timezone.utc)
    await db.flush()
    return {"connection_id": str(connection.id), **result}
