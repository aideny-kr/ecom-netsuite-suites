"""Celigo integrator.io REST client.

Plan A scope: token verification only. Pagination, field projection, and the
resource fetchers land in Plan B.

Two facts drive this module's shape:
  * EU accounts are fully isolated at api.eu.integrator.io. A US-region call
    against an EU account fails auth, so region is stored per connection and
    routed here.
  * Celigo returns {"message": ...} on 401/403, NOT the {"errors": [...]}
    envelope it uses elsewhere. Parsing the wrong shape turns a clean auth
    failure into a 500.
"""

from __future__ import annotations

import httpx

CELIGO_BASE_URLS: dict[str, str] = {
    "us": "https://api.integrator.io",
    "eu": "https://api.eu.integrator.io",
}

# Celigo's hosted MCP server -- a fixed URL per region, not tenant-configurable
# (Plan A). Derived from CELIGO_BASE_URLS (never a second hardcoded map) so
# the MCP host and the REST host can't drift apart: EU accounts are fully
# isolated (see module docstring), and the MCP server follows the exact same
# region split as the REST API. Lives here (not in connector_status.py, its
# original home) so mcp_connector_service.create_mcp_connector can import it
# without a circular import (connector_status.py already imports
# mcp_connector_service).
CELIGO_MCP_SERVER_URLS: dict[str, str] = {region: f"{url}/celigo-mcp" for region, url in CELIGO_BASE_URLS.items()}

# Backward-compatible single-URL constant (US only). Prefer mcp_server_url(region).
CELIGO_MCP_SERVER_URL = CELIGO_MCP_SERVER_URLS["us"]

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)


class CeligoAuthError(Exception):
    """The token was rejected by Celigo (401/403)."""


class CeligoError(Exception):
    """Celigo returned an unexpected non-2xx response."""


def base_url(region: str) -> str:
    """Return the API base URL for *region*, defaulting to US on an unknown value."""
    return CELIGO_BASE_URLS.get(region, CELIGO_BASE_URLS["us"])


def mcp_server_url(region: str) -> str:
    """Return Celigo's fixed hosted MCP server URL for *region*.

    Defaults to US on an unrecognized value -- mirrors base_url() exactly, so
    a caller-supplied region that fails validation upstream (or an unfamiliar
    value) never routes to "no URL" but always to a well-defined trusted
    constant.
    """
    return CELIGO_MCP_SERVER_URLS.get(region, CELIGO_MCP_SERVER_URLS["us"])


def _auth_message(response: httpx.Response) -> str:
    """Extract Celigo's auth error text, tolerating either envelope."""
    try:
        body = response.json()
    except ValueError:
        return response.text or "authentication failed"
    if isinstance(body, dict):
        if body.get("message"):
            return str(body["message"])
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                return str(first["message"])
    return "authentication failed"


async def verify_token(
    token: str,
    region: str = "us",
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Verify *token* against Celigo and return the account identity.

    Returns ``{"account_name": str, "user_email": str}``.
    Raises :class:`CeligoAuthError` on 401/403, :class:`CeligoError` otherwise.
    """
    url = f"{base_url(region)}/v1/tokenInfo"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await http.get(url, headers=headers)
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code in (401, 403):
        raise CeligoAuthError(_auth_message(response))
    # A genuine success is a 2xx status -- NOT merely "< 400". Without this,
    # a 3xx (e.g. a proxy/redirect Celigo never documents) or a 204 falls
    # through neither raise above and reaches the body-parsing code below,
    # which then degrades a non-response into a "verified" empty identity.
    if not (200 <= response.status_code < 300):
        raise CeligoError(f"Celigo returned {response.status_code}")

    try:
        body = response.json()
    except ValueError:
        raise CeligoError("Celigo returned an unparseable token-info response") from None
    if not isinstance(body, dict):
        raise CeligoError("Celigo returned an unparseable token-info response")

    account_name = str(body.get("name") or "")
    user_email = str(body.get("email") or "")
    # A 2xx with NEITHER field proves nothing about the token -- this used to
    # silently degrade to {"account_name": "", "user_email": ""} and let the
    # caller treat that as "verified". Not a CeligoAuthError: this isn't
    # evidence the token was rejected, only that Celigo's response carried no
    # identity to report.
    if not account_name and not user_email:
        raise CeligoError("Celigo returned no identity for this token")

    return {"account_name": account_name, "user_email": user_email}
