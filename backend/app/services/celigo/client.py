"""Celigo integrator.io REST client.

Plan A scope: token verification. Plan B (Task 3) adds paginated, projected
resource fetchers: `list_resource`, `get_resource`, `list_flow_errors_for_step`.
`list_error_summary_for_integration` also exists for signature compatibility
but DELIBERATELY ALWAYS RAISES -- see its own docstring (FIX ROUND 1) for why
there is no verified primitive to build it on yet.

Two facts drive Plan A's shape:
  * EU accounts are fully isolated at api.eu.integrator.io. A US-region call
    against an EU account fails auth, so region is stored per connection and
    routed here.
  * Celigo returns {"message": ...} on 401/403, NOT the {"errors": [...]}
    envelope it uses elsewhere. Parsing the wrong shape turns a clean auth
    failure into a 500.

TASK 3 SECURITY MODEL (see sanitizer.py's module docstring for the full
story): a live probe found Celigo ignores BOTH `include` and `exclude` for
payload-bearing fields -- `exclude=mockResponse` on a real import still
returned it; a positive `include=` allowlist on a real export still returned
`mockOutput`/`rawData` unrequested. Projection is NOT a privacy control in
either direction; it is passed through anyway (it shrinks most responses and
costs nothing) but relied on for NOTHING. Every fetcher in this module runs
its response through `sanitize()` before returning -- unconditionally, on
every branch, including partial/error-adjacent ones -- so a raw Celigo object
can never leave this module. No fetcher here logs a response body; the only
things logged (via exception messages) are status codes and resource
identifiers, never payload content.

TASK 3 PAGINATION: Celigo's documented pagination
(developer.celigo.com/api/using-the-api/pagination.md, fetched 2026-08-27 --
NOT independently verified live, see task-3-report.md) is Link-header based
for collection endpoints (`Link: <url>; rel="next"`, follow verbatim, never
hand-craft `after`/`limit`; a 204 with no body means nothing to list) and
body-based (`{"errors": [...], "nextPageURL": "..."}`) for the per-step flow
error endpoint specifically -- the docs' own worked example for body-based
pagination IS that errors endpoint. `list_resource`/`get_resource` use the
former; `list_flow_errors_for_step` uses the latter.

TASK 3 ERRORS ARE PER-STEP, NOT PER-FLOW (observed-shapes.md, live-probed
2026-08-27): passing a flow id alone (no step id) returns `steps: []` even
when errors exist -- the useless mode. `list_flow_errors_for_step` makes that
mode structurally unreachable: `flow_id` and `step_id` are both required
positional parameters, never optional.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

import httpx

from app.services.celigo.sanitizer import sanitize

CELIGO_BASE_URLS: dict[str, str] = {
    "us": "https://api.integrator.io",
    "eu": "https://api.eu.integrator.io",
}

# Resource kinds `list_resource`/`get_resource` know how to fetch, mapped to
# their `/v1/<endpoint>` collection name. Deliberately excludes "error":
# errors are not a standalone listable/gettable resource in Celigo's API --
# they only exist scoped to a flow + step, which is why they get their own
# dedicated functions below rather than going through this generic path.
_KIND_ENDPOINTS: dict[str, str] = {
    "integration": "integrations",
    "flow": "flows",
    "export": "exports",
    "import": "imports",
    "script": "scripts",
}

# Bound on 429 retries and on error-page-following. Both are read-only GET
# loops driven by server-supplied cursors, but neither should be allowed to
# spin forever against a misbehaving or malicious response.
_MAX_RETRIES = 3
_MAX_ERROR_PAGES = 200

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

    user_id = str(body.get("_userId") or "")
    scope = str(body.get("scope") or "")
    # ``name``/``email`` are NOT returned by this endpoint. They are read
    # opportunistically for display only, and are empty for every real token.
    account_name = str(body.get("name") or "")
    user_email = str(body.get("email") or "")

    # ``_userId`` is the only identity field Celigo documents for /v1/tokenInfo
    # (https://github.com/celigo/integrator-api-docs) -- a real service token
    # returns exactly {"_userId": "..."}, optionally with "scope".
    #
    # This guard used to key on name/email, which the endpoint never sends, so
    # it raised for EVERY valid token and the connector could not be connected
    # at all. The whole suite stayed green because every fixture invented those
    # two fields. Keying on ``_userId`` both fixes that and gives callers a
    # stable id to compare rather than a display string Celigo never promised
    # to be unique -- see ``_celigo_accounts_match``.
    if not user_id:
        raise CeligoError("Celigo returned no identity for this token")

    return {
        "user_id": user_id,
        "scope": scope,
        "account_name": account_name,
        "user_email": user_email,
    }


def _raise_for_status(response: httpx.Response, *, context: str) -> None:
    """Shared status check for the Task 3 fetchers. Mirrors verify_token's
    401/403-vs-other split: an auth failure is caller error (bad/expired
    token), everything else is treated as an upstream problem. Never
    includes the response body in the raised message -- only the status
    code and a caller-supplied, payload-free description of what was being
    fetched -- so a raw Celigo object can never end up in a traceback."""
    if response.status_code in (401, 403):
        raise CeligoAuthError(_auth_message(response))
    if not (200 <= response.status_code < 300):
        raise CeligoError(f"Celigo returned {response.status_code} while {context}")


def _retry_after_seconds(response: httpx.Response, *, default: float = 1.0) -> float:
    """Best-effort parse of a 429's Retry-After header. RFC 7231 allows
    either delta-seconds (what Celigo sends in every case observed) or an
    HTTP-date; a non-numeric value falls back to *default* rather than
    raising -- a malformed backoff hint must never crash the retry loop."""
    value = response.headers.get("retry-after")
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default


async def _get_with_retry(
    http: httpx.AsyncClient,
    url: str,
    *,
    headers: dict,
    params: dict | None = None,
) -> httpx.Response:
    """GET *url*, honouring a 429's Retry-After by sleeping and retrying up
    to `_MAX_RETRIES` times. The final response (whatever its status) is
    returned to the caller, which decides success/failure via
    `_raise_for_status` -- this function's only job is the retry loop."""
    attempt = 0
    while True:
        response = await http.get(url, headers=headers, params=params)
        if response.status_code == 429 and attempt < _MAX_RETRIES:
            await asyncio.sleep(_retry_after_seconds(response))
            attempt += 1
            continue
        return response


def _join_projection(value: str | Iterable[str]) -> str:
    """`include`/`exclude` accept either a pre-joined comma string or an
    iterable of field names, for caller convenience."""
    return value if isinstance(value, str) else ",".join(value)


def _resolve_next_page_url(next_page_url: str, region: str) -> str:
    """`nextPageURL` (the error endpoint's body-pagination field) may be
    absolute or relative per the documented example
    (`/v1/flows/.../errors?after=...`) -- resolve a relative one against
    this region's host rather than assuming a shape."""
    if next_page_url.startswith("http://") or next_page_url.startswith("https://"):
        return next_page_url
    path = next_page_url if next_page_url.startswith("/") else f"/{next_page_url}"
    return f"{base_url(region)}{path}"


async def list_resource(
    kind: str,
    *,
    token: str,
    region: str = "us",
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
    params: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[dict]:
    """List every resource of *kind*, transparently following Celigo's
    Link-header pagination (`rel="next"`) until it's absent, and sanitizing
    each item before it's yielded.

    *include*/*exclude* are passed through to the wire as-is when given --
    they shrink most responses and cost nothing -- but callers must NOT rely
    on them to keep payload-bearing fields off the wire; see the module
    docstring. *params* carries any other caller-supplied filter (e.g.
    ``{"_integrationId": ...}``).

    Raises :class:`ValueError` for an unrecognized *kind* -- a resource type
    this module has no allowlist for is not something we can safely fetch
    and sanitize, so this fails before making any request rather than after.

    Caller-owned resource note: when *client* is omitted, this generator
    creates and owns an `httpx.AsyncClient` for its lifetime and closes it
    once the generator is exhausted (or explicitly aclosed). A caller that
    only partially iterates and never closes the generator should pass its
    own *client* instead, the same convention `verify_token` already uses.
    """
    endpoint = _KIND_ENDPOINTS.get(kind)
    if endpoint is None:
        raise ValueError(f"unknown Celigo resource kind: {kind!r}")

    query: dict = dict(params or {})
    if include:
        query["include"] = _join_projection(include)
    if exclude:
        query["exclude"] = _join_projection(exclude)

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url: str | None = f"{base_url(region)}/v1/{endpoint}"
    # Only the FIRST request carries our query params -- the Link header's
    # `rel="next"` URL is a complete, self-sufficient URL (it already embeds
    # whatever filters were on the original query, plus the server's own
    # `after`/`limit`), so subsequent requests use it verbatim.
    next_params: dict | None = query
    try:
        while url:
            response = await _get_with_retry(http, url, headers=headers, params=next_params)
            next_params = None
            _raise_for_status(response, context=f"listing {kind}")
            if response.status_code == 204:
                return
            try:
                body = response.json()
            except ValueError:
                raise CeligoError(f"Celigo returned an unparseable {kind} listing") from None
            if not isinstance(body, list):
                raise CeligoError(f"Celigo returned a non-list body listing {kind}")
            for raw in body:
                if isinstance(raw, dict):
                    yield sanitize(kind, raw)
            url = response.links.get("next", {}).get("url")
    finally:
        if owns_client:
            await http.aclose()


async def get_resource(
    kind: str,
    celigo_id: str,
    *,
    token: str,
    region: str = "us",
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Fetch a single resource of *kind* by id and sanitize it before
    returning. See `list_resource` for the *include*/*exclude* caveat and
    the *kind* validation behavior -- both apply identically here."""
    endpoint = _KIND_ENDPOINTS.get(kind)
    if endpoint is None:
        raise ValueError(f"unknown Celigo resource kind: {kind!r}")

    query: dict = {}
    if include:
        query["include"] = _join_projection(include)
    if exclude:
        query["exclude"] = _join_projection(exclude)

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"{base_url(region)}/v1/{endpoint}/{celigo_id}"
    try:
        response = await _get_with_retry(http, url, headers=headers, params=query)
    finally:
        if owns_client:
            await http.aclose()

    _raise_for_status(response, context=f"fetching {kind} {celigo_id}")
    try:
        body = response.json()
    except ValueError:
        raise CeligoError(f"Celigo returned an unparseable {kind} response") from None
    if not isinstance(body, dict):
        raise CeligoError(f"Celigo returned an unparseable {kind} response")
    return sanitize(kind, body)


async def list_flow_errors_for_step(
    flow_id: str,
    step_id: str,
    *,
    token: str,
    region: str = "us",
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """List every open error for one step of one flow, following the error
    endpoint's body-based pagination (`nextPageURL`) until it's absent, and
    sanitizing each error before it's returned.

    `flow_id` and `step_id` are BOTH required, non-optional parameters --
    deliberately, not incidentally. A live probe (observed-shapes.md,
    2026-08-27) found that querying by flow id alone returns `steps: []`
    even when errors exist for that flow; this signature makes that useless
    call structurally impossible to make through this function rather than
    merely discouraging it in a docstring.

    Returns a materialized `list`, not an async generator -- callers of this
    function need the full error set for one step (e.g. to fingerprint or
    dedupe), not a lazy stream.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url: str | None = f"{base_url(region)}/v1/flows/{flow_id}/errors"
    next_params: dict | None = {"_stepId": step_id}
    errors: list[dict] = []
    try:
        pages = 0
        while url and pages < _MAX_ERROR_PAGES:
            response = await _get_with_retry(http, url, headers=headers, params=next_params)
            next_params = None
            _raise_for_status(response, context=f"listing errors for flow {flow_id} step {step_id}")
            if response.status_code == 204:
                break
            try:
                body = response.json()
            except ValueError:
                raise CeligoError("Celigo returned an unparseable flow-errors response") from None
            if not isinstance(body, dict):
                raise CeligoError("Celigo returned an unparseable flow-errors response")
            for raw in body.get("errors") or []:
                if isinstance(raw, dict):
                    errors.append(sanitize("error", raw))
            next_page_url = body.get("nextPageURL")
            url = _resolve_next_page_url(next_page_url, region) if next_page_url else None
            pages += 1
    finally:
        if owns_client:
            await http.aclose()
    return errors


async def list_error_summary_for_integration(
    integration_id: str,
    *,
    token: str,
    region: str = "us",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """DELIBERATELY ALWAYS RAISES. Do not attempt to "fix" this by wiring it
    back up to `GET /v1/flows?_integrationId=` -- that was tried and it was
    wrong, not merely incomplete.

    FIX ROUND 1 (team lead, live-verified 2026-08-27): two live calls
    against the same three real flows, same order --
    `list_flows(limit=3, includeErrorCounts=true)` put `"numOpenError": 0` on
    every item; `list_flows(limit=3)` (plain, no `includeErrorCounts`) had NO
    `numOpenError` key at all, on ANY item. A plain `GET
    /v1/flows?_integrationId=` -- the only primitive this function originally
    had confirmed evidence for -- can therefore never carry a real error
    count. The first version of this function summed that always-absent
    field and returned `{"total_errors": 0, ...}` unconditionally -- a
    silent false negative for an account that has 103+ open errors across
    13+ flows right now. A monitoring feature that quietly reports zero
    problems is worse than one that reports nothing: Task 6/7 would build a
    "no errors to snapshot" path on it and nobody would ever see it fail.

    There is no verified REST primitive today that can honestly answer "how
    many open errors does this integration have". The only known populating
    path, `POST /v1/flows/runs/stats` (named only inside another MCP tool's
    own description of its internals), is undocumented on
    developer.celigo.com and was never confirmed against a real request or
    response. Building on it blind would repeat exactly the mistake this fix
    corrects, just moved one layer down.

    Kept as a signature-compatible stub (same params, same declared
    `-> dict`) rather than deleted, so a future task that DOES verify a real
    summary primitive can fill this in without changing every call site --
    but it fails loudly and immediately, before any request is made, until
    that happens. Callers today have two honest options: verify
    `POST /v1/flows/runs/stats` live first, or fan out per (flow_id,
    step_id) through `list_flow_errors_for_step` once step ids are known
    (e.g. from Task 5's flow-step extraction).
    """
    raise CeligoError(
        "list_error_summary_for_integration has no verified Celigo REST primitive to "
        "answer from: numOpenError is ABSENT from a plain GET /v1/flows?_integrationId= "
        "listing (live-verified 2026-08-27), and the only known populating path, "
        "POST /v1/flows/runs/stats, is undocumented and unverified. Use "
        "list_flow_errors_for_step(flow_id, step_id, ...) per step instead, or verify "
        "the stats endpoint live before relying on this function."
    )
