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
        t for t in tables - allowed_tables if not t.startswith("customrecord_") and not t.startswith("customlist_")
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


_SELECT_RE = re.compile(r"SELECT\s+(.*?)\s+FROM\s+", re.IGNORECASE)
_AS_RE = re.compile(r"\bAS\s+(\w+)\s*$", re.IGNORECASE)
_IDENT_RE = re.compile(r"^[a-zA-Z_]\w*$")
_DOT_RE = re.compile(r"\.(\w+)$")


def collect_columns(items: list[dict]) -> list[str]:
    """Extract unique column names from NetSuite API response items.

    Preserves insertion order, excludes the ``links`` metadata field,
    and handles NULL-key omission (different items may have different keys).
    """
    columns: list[str] = []
    seen: set[str] = set()
    for item in items:
        for k in item.keys():
            if k != "links" and k not in seen:
                columns.append(k)
                seen.add(k)
    return columns


def parse_select_aliases(query: str) -> list[str]:
    """Extract column aliases from the SELECT clause in query order.

    Handles: ``t.tranid``, ``t.tranid AS po_number``, ``BUILTIN.DF(t.status) AS status``,
    ``SUM(tl.quantity) AS total_qty``.  Returns lowercased alias names.
    """
    normalized = re.sub(r"\s+", " ", query.strip())
    select_match = _SELECT_RE.match(normalized)
    if not select_match:
        return []

    select_body = select_match.group(1)

    # Split by commas respecting parentheses depth
    expressions: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in select_body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            expressions.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        expressions.append("".join(current).strip())

    result: list[str] = []
    for expr in expressions:
        as_match = _AS_RE.search(expr)
        if as_match:
            result.append(as_match.group(1).lower())
            continue
        tokens = expr.strip().split()
        # Positional alias without AS keyword (e.g. "SUM(x) total_qty")
        if len(tokens) >= 2 and _IDENT_RE.match(tokens[-1]):
            result.append(tokens[-1].lower())
            continue
        # Single expression — extract last dotted part (e.g. t.tranid → tranid)
        clean = tokens[-1] if tokens else expr
        dot_match = _DOT_RE.search(clean)
        if dot_match:
            result.append(dot_match.group(1).lower())
        else:
            result.append(clean.lower())

    return result


def reorder_columns(api_columns: list[str], query: str) -> list[str]:
    """Reorder API-returned columns to match the SELECT clause order.

    Columns present in the SELECT aliases come first (in SELECT order),
    followed by any extra API columns not matched (preserving their order).
    """
    select_order = parse_select_aliases(query)
    if not select_order:
        return api_columns

    # Build case-insensitive lookup: lowered name → original name
    lower_to_orig = {c.lower(): c for c in api_columns}
    ordered: list[str] = []
    used: set[str] = set()

    for alias in select_order:
        if alias in lower_to_orig and alias not in used:
            ordered.append(lower_to_orig[alias])
            used.add(alias)

    # Append any remaining columns not in SELECT order
    for col in api_columns:
        if col.lower() not in used:
            ordered.append(col)

    return ordered


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


async def _maybe_judge(result: dict, user_question: str | None, query: str, importance_tier: int = 1) -> dict:
    """Run the SuiteQL judge if user_question is provided and result has rows."""
    if not user_question or result.get("error"):
        return result

    rows = result.get("rows", result.get("items", []))
    if not rows:
        return result

    try:
        from app.services.importance_classifier import ImportanceTier
        from app.services.suiteql_judge import enforce_judge_threshold, judge_suiteql_result

        verdict = await judge_suiteql_result(
            user_question=user_question,
            sql=query,
            result_preview=rows[:5],
            row_count=result.get("row_count", len(rows)),
        )

        tier = ImportanceTier(importance_tier)
        enforcement = enforce_judge_threshold(verdict, tier)

        result["judge_verdict"] = {
            "approved": verdict.approved,
            "confidence": verdict.confidence,
            "reason": enforcement.reason,
            "tier": enforcement.tier,
            "passed": enforcement.passed,
            "needs_review": enforcement.needs_review,
        }
        if not enforcement.passed:
            result["_judge_warning"] = f"[{enforcement.tier}] {enforcement.reason}"
    except Exception:
        pass  # Fail-open: judge errors don't block the result

    return result


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Execute a SuiteQL query against NetSuite via SuiteTalk REST API."""
    query: str = params.get("query", "")
    limit: int = params.get("limit", 100)
    user_question: str | None = params.get("user_question")
    # Investigation queries (FULL context) get 120s timeout for systemnote and complex JOINs
    context_need = context.get("context_need") if context else None
    default_timeout = 120 if context_need == "full" else settings.NETSUITE_SUITEQL_TIMEOUT
    timeout_seconds: int = params.get("timeout_seconds", default_timeout)

    if not query:
        return {"error": True, "message": "No query provided."}

    # Log the FULL query for debugging (SQLAlchemy engine logs truncate it)
    print(f"[SUITEQL] Full query received:\n{query}", flush=True)

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
    # Internal callers (e.g. financial_report tool) can set skip_limit_cap=True
    # via **kwargs to bypass the global max. NOT read from params to prevent
    # external MCP callers from bypassing the cap.
    skip_cap = kwargs.get("_skip_limit_cap", False)
    max_rows = limit if skip_cap else min(limit, settings.NETSUITE_SUITEQL_MAX_ROWS)
    query = enforce_limit(query, max_rows)

    print(f"[SUITEQL] Final query after enforce_limit (max_rows={max_rows}):\n{query}", flush=True)

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
            result = await execute_suiteql(
                access_token, account_id, query, max_rows,
                paginate=True,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            error_msg = str(exc)
            hint = ""
            if "400" in error_msg:
                hint = (
                    " HINT: A 400 error usually means an invalid column name in your query."
                    " Use netsuite_get_metadata or web_search to verify column names"
                    " before retrying. Do NOT guess — look it up."
                )
            return {"error": True, "message": f"NetSuite query failed: {error_msg}{hint}"}

        result = {**result, "query": query, "limit": max_rows}

        return await _maybe_judge(
            result,
            user_question,
            query,
            importance_tier=context.get("importance_tier", 1) if context else 1,
        )

    # --- OAuth 1.0 path: direct REST call with HMAC signature ---
    url = f"https://{account_id}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"

    auth_headers = build_oauth1_header(credentials, "POST", url)
    headers = {
        **auth_headers,
        "Content-Type": "application/json",
        "Prefer": "transient",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
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
    columns = reorder_columns(collect_columns(items), query)
    # Build rows aligned to columns — use None for missing keys (NULL omission)
    rows = [[item.get(col) for col in columns] for item in items]
    total_results = data.get("totalResults", len(rows))
    truncated = total_results > len(rows)

    result = {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "query": query,
        "limit": max_rows,
    }

    return await _maybe_judge(
        result,
        user_question,
        query,
        importance_tier=context.get("importance_tier", 1) if context else 1,
    )


# ──────────────────────────────────────────────────────────────────
# Permission diagnostics
# ──────────────────────────────────────────────────────────────────

# Tables where 0 rows almost certainly means a permissions problem
_DIAGNOSTIC_TABLES = {
    "transaction": "SELECT COUNT(*) as cnt FROM transaction FETCH FIRST 1 ROWS ONLY",
    "customer": "SELECT COUNT(*) as cnt FROM customer FETCH FIRST 1 ROWS ONLY",
    "item": "SELECT COUNT(*) as cnt FROM item FETCH FIRST 1 ROWS ONLY",
    "employee": "SELECT COUNT(*) as cnt FROM employee FETCH FIRST 1 ROWS ONLY",
    "vendor": "SELECT COUNT(*) as cnt FROM vendor FETCH FIRST 1 ROWS ONLY",
}

_PERMISSION_WARNING = (
    "⚠️ PERMISSION ISSUE DETECTED: Your NetSuite OAuth connection returned 0 rows "
    "for the '{table}' table. This typically means the Integration Record's role in "
    "NetSuite does not have permission to view these records.\n\n"
    "To fix this, please check your NetSuite setup:\n"
    "1. Go to Setup > Integration > Manage Integrations and find your Integration Record\n"
    "2. Verify the assigned Role has '{table}' permissions (View or Full level)\n"
    "3. If using a custom role, ensure it includes: Transactions, Lists, Reports permissions\n"
    "4. Alternatively, assign the Administrator role to the Integration Record\n\n"
    "Please relay this information to the user so they can fix their NetSuite connection settings."
)


async def _diagnose_empty_result(
    query: str,
    access_token: str,
    account_id: str,
) -> str | None:
    """Run a diagnostic count query when a user query returns 0 rows (OAuth 2.0)."""
    from app.services.netsuite_client import execute_suiteql

    tables = parse_tables(query)
    for table in tables:
        if table in _DIAGNOSTIC_TABLES:
            try:
                diag_result = await execute_suiteql(
                    access_token,
                    account_id,
                    _DIAGNOSTIC_TABLES[table],
                )
                items = diag_result.get("items", [])
                if not items or items[0].get("cnt", 0) == 0:
                    return _PERMISSION_WARNING.format(table=table)
            except Exception:
                pass
    return None


async def _diagnose_empty_result_oauth1(
    query: str,
    credentials: dict,
    url: str,
    account_id: str,
) -> str | None:
    """Run a diagnostic count query when a user query returns 0 rows (OAuth 1.0)."""
    tables = parse_tables(query)
    for table in tables:
        if table in _DIAGNOSTIC_TABLES:
            diag_query = _DIAGNOSTIC_TABLES[table]
            auth_headers = build_oauth1_header(credentials, "POST", url)
            headers = {
                **auth_headers,
                "Content-Type": "application/json",
                "Prefer": "transient",
            }
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, headers=headers, json={"q": diag_query})
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get("items", [])
                    if not items or items[0].get("cnt", 0) == 0:
                        return _PERMISSION_WARNING.format(table=table)
            except Exception:
                pass
    return None
