"""Minimal NetSuite SuiteQL REST client for the ns-suiteql MCP server.

Reads OAuth 2.0 Bearer credentials from a JSON config file at the path in
the `SUITE_STUDIO_NS_CONNECTION_FILE` env var (the sidecar sets this when
spawning the server). Calls SuiteTalk REST directly — no token refresh,
no DB lookup. Refresh-on-401 is deferred (operator re-mints out-of-band
and updates the file). Token storage is deliberately filesystem-only at
B0; keychain integration lands at the fifth /goal (see
Desktop-Architecture-v1 §5.1).

The OAuth 2.0 Bearer path mirrors `backend/app/services/netsuite_client.py`
without any of the Hosted-side wrappers (DB session, mutation guard, judge
loop, importance classifier). It is a fresh minimal implementation per
plan doc §Inputs ("do NOT port wholesale").

Functions are intentionally split between this module and `server.py` so
the FastMCP-tool wiring can be tested independently from the validation +
HTTP logic. Both modules sit in `desktop/runtime/mcp-servers/ns-suiteql/`
which is on the pythonpath under `[tool.pytest.ini_options]`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx

# Hard cap on rows returned per query. NetSuite's max page size is 1000;
# we stay well below to keep the Bearer-token-on-disk surface small and
# the MCP-tool payload bounded.
DEFAULT_MAX_ROWS = 100

# Placeholder marker the sidecar writes into the template file the
# operator hasn't filled in yet. If we see this in the bearer_token, the
# operator hasn't completed setup — fail with an actionable error rather
# than send a literal "REPLACE_ME" to NetSuite as a Bearer token.
PLACEHOLDER_MARKER = "REPLACE_ME"

# Env var the sidecar uses to point the server at the right
# ~/SuiteStudio/{org}/netsuite-connection.json. Configurable so future
# multi-org flows can swap orgs without restarting the server.
ENV_CONNECTION_FILE = "SUITE_STUDIO_NS_CONNECTION_FILE"


_WRITE_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "GRANT",
    "REVOKE",
    "MERGE",
}


def is_read_only_sql(query: str) -> bool:
    """Return True if every non-empty statement in `query` is a SELECT.

    Mirrors `backend/app/mcp/tools/netsuite_suiteql.py::is_read_only_sql`.
    The defensive split-on-semicolon catches multi-statement strings like
    "SELECT id FROM customer; DELETE FROM customer" — SuiteQL doesn't
    execute multiple statements per call, but the validator runs before
    the HTTP boundary so we don't have to trust NetSuite to reject them.
    """
    normalized = query.strip().upper()
    if not normalized.startswith("SELECT"):
        return False
    for stmt in normalized.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        first_word = stmt.split()[0] if stmt.split() else ""
        if first_word in _WRITE_KEYWORDS:
            return False
    return True


_FETCH_RE = re.compile(r"FETCH\s+FIRST\s+(\d+)\s+ROWS\s+ONLY", re.IGNORECASE)


def enforce_limit(query: str, max_rows: int) -> str:
    """Inject `FETCH FIRST max_rows ROWS ONLY` if absent, otherwise cap.

    SuiteQL uses Oracle pagination syntax (FETCH FIRST, never LIMIT) per
    the dialect rules in `desktop/skills/suite-studio-netsuite/suiteql/SKILL.md`.
    """
    stripped = query.rstrip().rstrip(";")
    match = _FETCH_RE.search(stripped)
    if match:
        existing = int(match.group(1))
        capped = min(existing, max_rows)
        return _FETCH_RE.sub(f"FETCH FIRST {capped} ROWS ONLY", stripped)
    return f"{stripped} FETCH FIRST {max_rows} ROWS ONLY"


def normalize_account_id(raw: str) -> str:
    """Lowercase + underscore→dash. SuiteTalk subdomain doesn't accept underscores."""
    return raw.replace("_", "-").lower()


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and PLACEHOLDER_MARKER in value


def load_connection(path: str) -> dict:
    """Read + validate `~/SuiteStudio/{org}/netsuite-connection.json`.

    Returns either `{"account_id": ..., "bearer_token": ..., ...}` on the
    happy path OR `{"error": True, "message": "..."}` on a structured
    failure. We do not raise — the FastMCP tool wrapper surfaces the
    error dict to the LLM so the agent can tell the operator what to do.
    """
    if not os.path.exists(path):
        return {
            "error": True,
            "message": (
                f"NetSuite connection file not found at {path}. "
                f"Run the sidecar once to create the placeholder, then populate it "
                f"with your NetSuite OAuth 2.0 Bearer token (out-of-band; never paste "
                f"a token into an agent prompt)."
            ),
        }

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "error": True,
            "message": f"Failed to read {path}: {exc}",
        }

    if not isinstance(data, dict):
        return {
            "error": True,
            "message": f"{path} must contain a JSON object, got {type(data).__name__}",
        }

    missing = [k for k in ("account_id", "bearer_token") if not data.get(k)]
    if missing:
        return {
            "error": True,
            "message": f"{path} is missing required keys: {', '.join(missing)}",
        }

    if _is_placeholder(data.get("bearer_token")) or _is_placeholder(data.get("account_id")):
        return {
            "error": True,
            "message": (
                f"{path} still contains placeholder values ('{PLACEHOLDER_MARKER}'). "
                f"Populate it with your real NetSuite Bearer token + account ID. "
                f"Operator-only — never paste creds into an agent prompt."
            ),
        }

    return data


def _suiteql_url(account_id: str) -> str:
    return (
        f"https://{normalize_account_id(account_id)}"
        f".suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    )


def _shape_response(payload: dict, executed_query: str, max_rows: int) -> dict:
    """Turn NetSuite's `items: [{col: val, ...}, ...]` into our columns+rows shape.

    Skips the `links` metadata field NetSuite appends to every item.
    """
    items = payload.get("items") or []
    columns: list[str] = []
    seen: set[str] = set()
    for item in items:
        for k in item.keys():
            if k != "links" and k not in seen:
                columns.append(k)
                seen.add(k)
    rows = [[item.get(col) for col in columns] for item in items]
    total = payload.get("totalResults", len(rows))
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": total > len(rows),
        "query": executed_query,
        "limit": max_rows,
    }


async def run_query(
    query: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict:
    """Validate, enforce limit, and POST a SuiteQL query to NetSuite.

    `transport` is a test seam — pass an `httpx.MockTransport` from tests
    to intercept the request without spawning a real HTTP connection.
    In production, leave it None and httpx uses its default transport.
    """
    if not query or not query.strip():
        return {"error": True, "message": "Query is empty."}

    if not is_read_only_sql(query):
        return {
            "error": True,
            "message": (
                "Only read-only SELECT queries are permitted by ns-suiteql. "
                "Mutation tools (ns_createRecord, ns_updateRecord) require the "
                "HITL flow which is not wired in this slice — see plan /goal #6."
            ),
        }

    connection_path = os.environ.get(ENV_CONNECTION_FILE)
    if not connection_path:
        return {
            "error": True,
            "message": (
                f"{ENV_CONNECTION_FILE} env var is not set. "
                f"The sidecar normally sets this when spawning the MCP server. "
                f"If running the server directly, point it at a populated "
                f"~/SuiteStudio/{{org}}/netsuite-connection.json file."
            ),
        }

    creds = load_connection(connection_path)
    if creds.get("error"):
        return creds

    bounded_query = enforce_limit(query, max_rows)
    url = _suiteql_url(creds["account_id"])
    headers = {
        "Authorization": f"Bearer {creds['bearer_token']}",
        "Content-Type": "application/json",
        "Prefer": "transient",
    }

    client_kwargs: dict[str, Any] = {"timeout": 60}
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, headers=headers, json={"q": bounded_query})
    except httpx.RequestError as exc:
        return {"error": True, "message": f"NetSuite request failed: {exc}"}

    if response.status_code >= 400:
        body = response.text[:500]
        return {
            "error": True,
            "message": (
                f"NetSuite returned HTTP {response.status_code}: {body}"
                if response.status_code != 401
                else (
                    f"NetSuite returned 401 Unauthorized — the Bearer token in "
                    f"{connection_path} is invalid or expired. Refresh it via the "
                    f"existing backend's OAuth flow and update the file. Response: {body}"
                )
            ),
        }

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        return {
            "error": True,
            "message": f"NetSuite returned non-JSON response (HTTP {response.status_code}): {exc}",
        }

    return _shape_response(payload, bounded_query, max_rows)
