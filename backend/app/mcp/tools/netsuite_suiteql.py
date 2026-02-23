"""Real SuiteQL execution tool for NetSuite SuiteTalk REST API.

Supports both OAuth 1.0 (consumer key / token) and OAuth 2.0 PKCE (access_token / refresh_token)
credential types. The auth_type field in decrypted credentials determines the path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse
import uuid

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.encryption import decrypt_credentials
from app.models.connection import Connection


def is_read_only_sql(query: str) -> bool:
    """Check if a SQL query is read-only (SELECT only)."""
    normalized = query.strip().upper()
    if not normalized.startswith("SELECT"):
        return False
    forbidden = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE"}
    statements = normalized.split(";")
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        first_word = stmt.split()[0] if stmt.split() else ""
        if first_word in forbidden:
            return False
    return True


def parse_tables(query: str) -> set[str]:
    """Extract table names referenced in FROM and JOIN clauses."""
    normalized = re.sub(r"\s+", " ", query.strip())
    pattern = r"(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)"
    matches = re.findall(pattern, normalized, re.IGNORECASE)
    return {m.lower() for m in matches}


def validate_query(query: str, allowed_tables: set[str]) -> None:
    """Validate that the query is read-only and only touches allowed tables.

    Custom record tables (customrecord_*) and custom list tables (customlist_*)
    are dynamically allowed since each tenant has different custom records.
    """
    if not is_read_only_sql(query):
        raise ValueError("Only read-only SELECT queries are permitted.")

    tables = parse_tables(query)
    # Allow any custom record/list table dynamically
    disallowed = {
        t for t in tables - allowed_tables
        if not t.startswith("customrecord_") and not t.startswith("customlist_")
    }
    if disallowed:
        raise ValueError(
            f"Query references disallowed tables: {', '.join(sorted(disallowed))}. "
            f"Allowed tables: {', '.join(sorted(allowed_tables))}"
        )


def enforce_limit(query: str, max_rows: int) -> str:
    """Inject or cap a row limit on the query.

    Uses FETCH FIRST N ROWS ONLY syntax expected by SuiteQL.
    If the query already contains a FETCH FIRST or LIMIT clause, cap
    the existing value at max_rows. Otherwise append FETCH FIRST.
    """
    stripped = query.rstrip().rstrip(";")

    # Check for existing FETCH FIRST ... ROWS ONLY
    fetch_pattern = re.compile(r"FETCH\s+FIRST\s+(\d+)\s+ROWS\s+ONLY", re.IGNORECASE)
    fetch_match = fetch_pattern.search(stripped)
    if fetch_match:
        existing = int(fetch_match.group(1))
        capped = min(existing, max_rows)
        return fetch_pattern.sub(f"FETCH FIRST {capped} ROWS ONLY", stripped)

    # Check for existing LIMIT clause
    limit_pattern = re.compile(r"LIMIT\s+(\d+)", re.IGNORECASE)
    limit_match = limit_pattern.search(stripped)
    if limit_match:
        existing = int(limit_match.group(1))
        capped = min(existing, max_rows)
        return limit_pattern.sub(f"LIMIT {capped}", stripped)

    # No limit present — append FETCH FIRST
    return f"{stripped} FETCH FIRST {max_rows} ROWS ONLY"


def build_oauth1_header(credentials: dict, method: str, url: str) -> dict[str, str]:
    """Build an OAuth 1.0 Authorization header using HMAC-SHA256.

    credentials keys: account_id, consumer_key, consumer_secret,
                      token_id, token_secret
    """
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()))

    oauth_params = {
        "oauth_consumer_key": credentials["consumer_key"],
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": timestamp,
        "oauth_token": credentials["token_id"],
        "oauth_version": "1.0",
    }

    # Build base string
    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in sorted(oauth_params.items())
    )
    base_string = "&".join(
        [
            method.upper(),
            urllib.parse.quote(url, safe=""),
            urllib.parse.quote(sorted_params, safe=""),
        ]
    )

    # Signing key
    signing_key = (
        urllib.parse.quote(credentials["consumer_secret"], safe="")
        + "&"
        + urllib.parse.quote(credentials["token_secret"], safe="")
    )

    # HMAC-SHA256 signature
    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    oauth_params["oauth_signature"] = signature
    realm = credentials["account_id"].replace("-", "_").upper()

    auth_header = "OAuth " + ", ".join(
        [f'realm="{realm}"'] + [f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(oauth_params.items())]
    )

    return {"Authorization": auth_header}


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Execute a SuiteQL query against NetSuite via SuiteTalk REST API."""
    query: str = params.get("query", "")
    limit: int = params.get("limit", 100)

    if not query:
        return {"error": True, "message": "No query provided."}

    # --- context is required for tenant_id and db session ---
    if not context:
        return {
            "error": True,
            "message": "Missing context — tenant_id and db session are required.",
        }

    tenant_id = context.get("tenant_id")
    db = context.get("db")
    if not tenant_id or not db:
        return {
            "error": True,
            "message": "Context must include tenant_id and db.",
        }

    # --- Look up the active NetSuite connection for this tenant ---
    try:
        result = await db.execute(
            select(Connection).where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status == "active",
            )
        )
        connection = result.scalars().first()
        if not connection:
            return {
                "error": True,
                "message": "No active NetSuite connection found for this tenant.",
            }
    except Exception as exc:
        return {"error": True, "message": f"DB lookup failed: {exc}"}

    # --- Decrypt credentials ---
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
    except Exception as exc:
        return {"error": True, "message": f"Failed to decrypt credentials: {exc}"}

    # --- Validate query ---
    allowed_tables = {t.strip().lower() for t in settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")}
    try:
        validate_query(query, allowed_tables)
    except ValueError as exc:
        return {"error": True, "message": str(exc)}

    # --- Enforce limit ---
    max_rows = min(limit, settings.NETSUITE_SUITEQL_MAX_ROWS)
    query = enforce_limit(query, max_rows)

    account_id = credentials["account_id"].replace("_", "-").lower()
    auth_type = credentials.get("auth_type", "oauth1")

    # --- OAuth 2.0 path: use netsuite_client with MCP→REST fallback ---
    if auth_type == "oauth2":
        from app.services.netsuite_client import execute_suiteql
        from app.services.netsuite_oauth_service import get_valid_token

        access_token = await get_valid_token(db, connection)
        if not access_token:
            return {
                "error": True,
                "message": "OAuth 2.0 token expired and refresh failed. Re-authorize.",
            }

        try:
            result = await execute_suiteql(access_token, account_id, query, max_rows)
        except Exception as exc:
            return {"error": True, "message": f"NetSuite query failed: {exc}"}

        return {
            **result,
            "query": query,
            "limit": max_rows,
        }

    # --- OAuth 1.0 path: direct REST call with HMAC signature ---
    url = f"https://{account_id}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"

    auth_headers = build_oauth1_header(credentials, "POST", url)
    headers = {
        **auth_headers,
        "Content-Type": "application/json",
        "Prefer": "transient",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.NETSUITE_SUITEQL_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json={"q": query})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response else ""
        return {
            "error": True,
            "message": f"NetSuite API error {exc.response.status_code}: {body}",
        }
    except httpx.RequestError as exc:
        return {
            "error": True,
            "message": f"NetSuite request failed: {exc}",
        }

    data = response.json()
    items = data.get("items", [])
    columns = list(items[0].keys()) if items else []
    rows = [list(item.values()) for item in items]
    total_results = data.get("totalResults", len(rows))
    truncated = total_results > len(rows)

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "query": query,
        "limit": max_rows,
    }
