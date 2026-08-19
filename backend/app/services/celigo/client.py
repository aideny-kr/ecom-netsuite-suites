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

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)


class CeligoAuthError(Exception):
    """The token was rejected by Celigo (401/403)."""


class CeligoError(Exception):
    """Celigo returned an unexpected non-2xx response."""


def base_url(region: str) -> str:
    """Return the API base URL for *region*, defaulting to US on an unknown value."""
    return CELIGO_BASE_URLS.get(region, CELIGO_BASE_URLS["us"])


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
    if response.status_code >= 400:
        raise CeligoError(f"Celigo returned {response.status_code}")

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    return {
        "account_name": str(body.get("name") or ""),
        "user_email": str(body.get("email") or ""),
    }
