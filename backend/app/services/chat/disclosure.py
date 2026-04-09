"""Disclosure footer for the v0 intent clarification surface.

Produces a post-tool `DisclosureBlock` describing what the agent just did:
- Which data source was read
- How the query was interpreted (relative dates, etc.)
- Which implicit filters were applied (cancelled, test, subsidiary, etc.)
- Whether the user can switch to the other data source

Designed to run synchronously after the last successful data tool call,
adding ~20-50ms to the turn. Never blocks user-visible text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class DisclosureBlock:
    """Post-tool disclosure block rendered as a footer under the assistant answer."""

    source: Literal["netsuite", "bigquery"]
    interpretation: str  # one human sentence describing how the query was parsed
    implicit_filters: list[str] = field(default_factory=list)
    can_switch_source: bool = False
    is_rerun: bool = False  # set when emitted after a source switch
    failure_mode: bool = False  # set when emitted under an error message

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "interpretation": self.interpretation,
            "implicit_filters": list(self.implicit_filters),
            "can_switch_source": self.can_switch_source,
            "is_rerun": self.is_rerun,
            "failure_mode": self.failure_mode,
        }


import re
from dataclasses import dataclass as _dataclass
from datetime import datetime, timedelta, timezone  # noqa: F401
from uuid import UUID


@_dataclass
class ParsedWhere:
    """Output of parse_where_clause — interpretation string + filter bullets."""

    interpretation: str
    filters: list[str]


# ── Regex constants ──────────────────────────────────────────────────────────

# Explicit date ranges: trandate >= 'YYYY-MM-DD' ... trandate <= 'YYYY-MM-DD'
_DATE_LITERAL_RE = re.compile(
    r"TO_DATE\s*\(\s*'(\d{4}-\d{2}-\d{2})'",
    re.IGNORECASE,
)
# Generic YYYY-MM-DD anywhere near trandate
_TRANDATE_RANGE_RE = re.compile(
    r"trandate\s*>=\s*[^,)]*?(\d{4}-\d{2}-\d{2}).*?trandate\s*<=\s*[^,)]*?(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)

# Relative dates (SuiteQL): TRUNC(SYSDATE, 'WW'|'MM'|'YYYY'|'DD'|'Q')
_RELATIVE_DATE_RE = re.compile(
    r"TRUNC\s*\(\s*SYSDATE\s*,\s*'(WW|IW|MM|YYYY|DD|Q)'\s*\)",
    re.IGNORECASE,
)

# BigQuery _TABLE_SUFFIX BETWEEN 'YYYYMMDD' AND 'YYYYMMDD'
_TABLE_SUFFIX_RE = re.compile(
    r"_TABLE_SUFFIX\s+BETWEEN\s+'(\d{8})'\s+AND\s+'(\d{8})'",
    re.IGNORECASE,
)

# Type equality / IN
_TYPE_EQ_RE = re.compile(r"\btype\s*=\s*'([^']+)'", re.IGNORECASE)
_TYPE_IN_RE = re.compile(r"\btype\s+IN\s*\(([^)]+)\)", re.IGNORECASE)

# Status equality / IN
_STATUS_EQ_RE = re.compile(r"\bstatus\s*=\s*'([^']+)'", re.IGNORECASE)
_STATUS_IN_RE = re.compile(r"\bstatus\s+IN\s*\(([^)]+)\)", re.IGNORECASE)

# Subsidiary / currency / entity equality
_SUBSIDIARY_RE = re.compile(r"\bsubsidiary\s*=\s*(\d+)", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"\bcurrency\s*=\s*(\d+)", re.IGNORECASE)
_ENTITY_RE = re.compile(r"\bentity\s*=\s*(\d+)", re.IGNORECASE)

# Boolean flags / IS NULL
_IS_TEST_RE = re.compile(r"\bis_test\s*=\s*(true|false)", re.IGNORECASE)
_CANCELLED_NULL_RE = re.compile(r"\bcancelled_at\s+IS\s+NULL", re.IGNORECASE)

# LIMIT / FETCH FIRST
_FETCH_FIRST_RE = re.compile(r"FETCH\s+FIRST\s+(\d+)\s+ROWS\s+ONLY", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)


# Friendly labels for SuiteQL "type" codes (see knowledge/golden_dataset/transaction-types-and-statuses.md)
_TRANSACTION_TYPE_LABELS = {
    "SalesOrd": "sales orders",
    "CashSale": "cash sales",
    "CustInvc": "invoices",
    "CustPymt": "customer payments",
    "CustCred": "credit memos",
    "VendBill": "vendor bills",
    "VendPymt": "vendor payments",
    "Journal": "journal entries",
    "Deposit": "deposits",
    "RtnAuth": "return authorizations",
    "CustRfnd": "customer refunds",
}

_RELATIVE_LABEL = {
    "WW": "this week",
    "IW": "this week",
    "MM": "this month",
    "YYYY": "this year",
    "DD": "today",
    "Q": "this quarter",
}


def parse_where_clause(sql: str) -> ParsedWhere:
    """Best-effort WHERE-clause parser.

    Returns a ParsedWhere with an interpretation sentence and a list of
    filter bullets. Silently drops anything it doesn't understand.
    """
    if not sql or not isinstance(sql, str):
        return ParsedWhere(interpretation="", filters=[])

    interpretation_parts: list[str] = []
    filters: list[str] = []

    # ── Temporal predicates ──
    rel = _RELATIVE_DATE_RE.search(sql)
    if rel:
        label = _RELATIVE_LABEL.get(rel.group(1).upper(), "")
        if label:
            interpretation_parts.append(f'"{label}" = current {label.replace("this ", "")}')

    explicit = _TRANDATE_RANGE_RE.search(sql)
    if explicit:
        start, end = explicit.group(1), explicit.group(2)
        interpretation_parts.append(f"Range {start} to {end}")
    else:
        literals = _DATE_LITERAL_RE.findall(sql)
        if len(literals) >= 2:
            interpretation_parts.append(f"Range {literals[0]} to {literals[-1]}")

    ts = _TABLE_SUFFIX_RE.search(sql)
    if ts:

        def _fmt(d: str) -> str:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"

        interpretation_parts.append(f"Partition range {_fmt(ts.group(1))} to {_fmt(ts.group(2))}")

    # ── Type filter ──
    type_eq = _TYPE_EQ_RE.search(sql)
    if type_eq:
        code = type_eq.group(1)
        label = _TRANSACTION_TYPE_LABELS.get(code, code)
        interpretation_parts.append(f"Limited to {label}")

    type_in = _TYPE_IN_RE.search(sql)
    if type_in:
        raw = type_in.group(1)
        codes = [c.strip().strip("'") for c in raw.split(",")]
        labels = [_TRANSACTION_TYPE_LABELS.get(c, c) for c in codes if c]
        if labels:
            interpretation_parts.append("Limited to " + ", ".join(labels))

    # ── Status filter ──
    status_eq = _STATUS_EQ_RE.search(sql)
    if status_eq:
        filters.append(f"Status = {status_eq.group(1)}")
    status_in = _STATUS_IN_RE.search(sql)
    if status_in:
        codes = [c.strip().strip("'") for c in status_in.group(1).split(",") if c.strip()]
        filters.append("Status in " + ", ".join(codes))

    # ── Subsidiary / currency / entity ──
    sub = _SUBSIDIARY_RE.search(sql)
    if sub:
        filters.append(f"Subsidiary {sub.group(1)} only")
    cur = _CURRENCY_RE.search(sql)
    if cur:
        filters.append(f"Currency {cur.group(1)} only")
    ent = _ENTITY_RE.search(sql)
    if ent:
        filters.append(f"Entity/customer {ent.group(1)} only")

    # ── Boolean + NULL flags ──
    is_test = _IS_TEST_RE.search(sql)
    if is_test:
        if is_test.group(1).lower() == "false":
            filters.append("Excludes test records")
        else:
            filters.append("Test records only")

    if _CANCELLED_NULL_RE.search(sql):
        filters.append("Excludes cancelled records")

    # ── LIMIT / FETCH FIRST ──
    ff = _FETCH_FIRST_RE.search(sql)
    if ff:
        filters.append(f"First {ff.group(1)} rows only")
    elif _LIMIT_RE.search(sql):
        lim = _LIMIT_RE.search(sql)
        filters.append(f"First {lim.group(1)} rows only")

    return ParsedWhere(
        interpretation=". ".join(interpretation_parts).strip(),
        filters=filters,
    )


# ── Query classification ────────────────────────────────────────────────────

# Keyword buckets — deliberately simple, order matters: saved_search before orders
# so "saved search for orders" matches saved_search.
_CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gl": (
        "journal",
        "journal entries",
        "gl balance",
        "gl account",
        "trial balance",
        "close the month",
        "close the period",
        "period end",
        "period-end",
        "accounting period",
        "month-end close",
        "chart of accounts",
        "accruals",
    ),
    "marketing": (
        "ad spend",
        "attribution",
        "marketing",
        "sessions",
        "cohort",
        "campaign",
        "utm",
        "ctr",
        "roas",
        "cac",
        "funnel",
    ),
    "saved_search": (
        "saved search",
        "custom record",
        "customrecord",
        "suitescript",
        "script id",
        "scriptid",
    ),
    "orders": (
        "order",
        "sales",
        "transaction",
        "customer",
        "item",
        "invoice",
        "payment",
        "refund",
        "return",
        "deposit",
        "sku",
        "shipment",
        "fulfillment",
    ),
}

# Classes where a source switch is viable.
DUAL_SOURCE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "orders": ("netsuite", "bigquery"),
    "gl": ("netsuite",),
    "marketing": ("bigquery",),
    "saved_search": ("netsuite",),
    "unmatched": (),
}


def classify_query_class(question: str) -> str:
    """Classify a user question into one of: orders, gl, marketing, saved_search, unmatched."""
    if not question:
        return "unmatched"
    q = question.lower()
    # Check more specific classes first (gl, marketing, saved_search) before orders,
    # since a "journal" query mentioning "transactions" is still GL.
    for klass in ("gl", "marketing", "saved_search", "orders"):
        for kw in _CLASS_KEYWORDS[klass]:
            if kw in q:
                return klass
    return "unmatched"


# ── Connector health (stubs patched by tests; real lookups added in later tasks) ──


def _tenant_has_connector(tenant_id: UUID, source: str) -> bool:
    """Default stub: returns False. Overridden by the async wrapper in assemble_disclosure."""
    return False


def _connector_is_healthy(tenant_id: UUID, source: str) -> bool:
    return False


def _bigquery_sync_age_hours(tenant_id: UUID) -> float:
    return 999.0


def compute_can_switch_source(
    current_source: str,
    tenant_id: UUID,
    query_class: str,
) -> bool:
    """Return True iff the user can switch to the *other* data source for this query."""
    other = "bigquery" if current_source == "netsuite" else "netsuite"

    allowed_sources = DUAL_SOURCE_ALLOWLIST.get(query_class, ())
    if other not in allowed_sources:
        return False
    if not _tenant_has_connector(tenant_id, other):
        return False
    if not _connector_is_healthy(tenant_id, other):
        return False
    if other == "bigquery" and _bigquery_sync_age_hours(tenant_id) > 24:
        return False
    return True


# ── Source-switch command regex ─────────────────────────────────────────────

_SOURCE_SWITCH_RE = re.compile(
    r"^\s*(?:use|switch\s+to|run\s+on|try)\s+(netsuite|bigquery|bq|ns)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_SOURCE_ALIASES = {"bq": "bigquery", "ns": "netsuite"}


# ── Pushback detection ──────────────────────────────────────────────────────

_PUSHBACK_RE = re.compile(
    r"^\s*(?:that'?s?\s+(?:wrong|not\s+right)|no,?\s+i\s+meant|actually|why\s+is|i\s+need)",
    re.IGNORECASE,
)


# Tools that fire a disclosure when they succeed
_DATA_TOOLS: frozenset[str] = frozenset(
    {
        "netsuite_suiteql",
        "bigquery_sql",
        "netsuite_financial_report",
        "pivot.query_result",
        "pivot_query_result",
        "ns_runCustomSuiteQL",
        "ns_getRecord",
        "ns_runSavedSearch",
        "saved_search",
    }
)

# Tools that are explicitly NON-data (metadata / writes / workspace)
_NON_DATA_TOOLS: frozenset[str] = frozenset(
    {
        "bigquery_schema",
        "bigquery_cost_estimate",
        "tenant_save_learned_rule",
        "ns_createRecord",
        "ns_updateRecord",
        "ns_deleteRecord",
    }
)


_NETSUITE_DATA_TOOLS: frozenset[str] = frozenset(
    {
        "netsuite_suiteql",
        "netsuite_financial_report",
        "ns_runCustomSuiteQL",
        "ns_getRecord",
        "ns_runSavedSearch",
        "saved_search",
        "pivot.query_result",
        "pivot_query_result",
    }
)


def _tool_source(tool_name: str) -> Literal["netsuite", "bigquery"] | None:
    if tool_name.startswith("bigquery"):
        return "bigquery"
    if tool_name in _NETSUITE_DATA_TOOLS:
        return "netsuite"
    return None


def _call_succeeded(call: dict) -> bool:
    result = call.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("success") is False:
        return False
    if "error" in result and result.get("success") is not True:
        return False
    return True


def assemble_disclosure(
    *,
    user_question: str,
    tool_calls_log: list[dict],
    current_source: Literal["netsuite", "bigquery"],
    tenant_id: UUID | None,
    matched_pattern_age_days: float | None = None,
    connector_checks: dict | None = None,
    is_rerun: bool = False,
) -> DisclosureBlock | None:
    """Assemble a DisclosureBlock for the current turn, or return None.

    Logic (per spec):
    1. No data-returning tool → None
    2. Fresh pattern (<7 days) → None
    3. All tool calls failed → failure-mode footer if can_switch_source, else None
    4. Otherwise → parse last successful data tool's WHERE clause → block

    Args:
        user_question: The raw user message for the current turn.
        tool_calls_log: Ordered list of {tool, input, result} dicts from the agent.
        current_source: Which source the agent routed to for this turn.
        tenant_id: Tenant UUID for connector health checks.
        matched_pattern_age_days: Age of the matched tenant_query_pattern in days,
            or None if no pattern matched.
        connector_checks: Dict of lambdas for connector health lookups. Keys:
            - tenant_has_connector(src) -> bool
            - connector_is_healthy(src) -> bool
            - bigquery_sync_age_hours() -> float
        is_rerun: True when this turn was spawned by a source-switch re-run.
    """
    # global must be declared before any use of the names in this scope
    global _tenant_has_connector, _connector_is_healthy, _bigquery_sync_age_hours

    # Filter out non-data tool calls
    data_calls = [c for c in tool_calls_log if c.get("tool") in _DATA_TOOLS and c.get("tool") not in _NON_DATA_TOOLS]

    if not data_calls:
        return None

    # Proven pattern suppression
    if matched_pattern_age_days is not None and matched_pattern_age_days < 7:
        return None

    # Wire connector checks into module-level stubs for compute_can_switch_source
    checks = connector_checks or {}
    has_fn = checks.get("tenant_has_connector", _tenant_has_connector)
    healthy_fn = checks.get("connector_is_healthy", _connector_is_healthy)
    sync_fn = checks.get("bigquery_sync_age_hours", _bigquery_sync_age_hours)

    # Temporarily bind module-level stubs so compute_can_switch_source picks them up.
    # Safe since this function is sync and single-threaded per turn.
    _prev = (_tenant_has_connector, _connector_is_healthy, _bigquery_sync_age_hours)
    _tenant_has_connector = lambda tid, src: has_fn(src)  # noqa: E731
    _connector_is_healthy = lambda tid, src: healthy_fn(src)  # noqa: E731
    _bigquery_sync_age_hours = lambda tid: sync_fn()  # noqa: E731
    try:
        query_class = classify_query_class(user_question)
        # When connector_checks are injected the patched stubs ignore tenant_id,
        # so use a sentinel UUID when tenant_id is None. When no checks are injected
        # and tenant_id is absent, skip the switch computation.
        if tenant_id is not None:
            can_switch = compute_can_switch_source(current_source, tenant_id, query_class)
        elif checks:
            _sentinel = UUID("00000000-0000-0000-0000-000000000000")
            can_switch = compute_can_switch_source(current_source, _sentinel, query_class)
        else:
            can_switch = False
    finally:
        _tenant_has_connector, _connector_is_healthy, _bigquery_sync_age_hours = _prev

    # All failed → failure-mode footer (only if can_switch)
    if not any(_call_succeeded(c) for c in data_calls):
        if not can_switch:
            return None
        return DisclosureBlock(
            source=current_source,
            interpretation=f"Tried {'NetSuite' if current_source == 'netsuite' else 'BigQuery'}.",
            implicit_filters=[],
            can_switch_source=True,
            is_rerun=is_rerun,
            failure_mode=True,
        )

    # Last successful data call is the "primary" call
    primary = next(c for c in reversed(data_calls) if _call_succeeded(c))
    sql = ""
    tool_input = primary.get("input") or {}
    if isinstance(tool_input, dict):
        sql = tool_input.get("query") or tool_input.get("sql") or ""

    parsed = parse_where_clause(sql)
    block_source: Literal["netsuite", "bigquery"] = _tool_source(primary.get("tool", "")) or current_source

    interpretation = parsed.interpretation or ""
    return DisclosureBlock(
        source=block_source,
        interpretation=interpretation,
        implicit_filters=parsed.filters,
        can_switch_source=can_switch,
        is_rerun=is_rerun,
        failure_mode=False,
    )


# ── Real connector lookups (async) ──────────────────────────────────────────


async def _build_connector_checks(db, tenant_id: UUID) -> dict:
    """Build a dict of lambdas that resolve real connector health for a tenant.

    Uses snapshots at call time so the sync assemble_disclosure function can
    consume them without needing an async context.
    """
    from sqlalchemy import select

    from app.models.connection import Connection
    from app.models.connection_alert import ConnectionAlert
    from app.models.mcp_connector import McpConnector

    # NetSuite connection (REST API)
    ns_result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
        )
    )
    has_netsuite = ns_result.scalar_one_or_none() is not None

    # BigQuery connector
    bq_result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "bigquery",
            McpConnector.is_enabled.is_(True),
        )
    )
    bq_connector = bq_result.scalar_one_or_none()
    has_bigquery = bq_connector is not None

    # Critical alerts (unhealthy connectors)
    alerts_result = await db.execute(
        select(ConnectionAlert).where(
            ConnectionAlert.tenant_id == tenant_id,
            ConnectionAlert.dismissed_at.is_(None),
        )
    )
    alerts = alerts_result.scalars().all()
    netsuite_unhealthy = any(a.connection_type in ("rest_api", "mcp") for a in alerts)

    # BigQuery sync age — from mcp_connectors.metadata_json["last_sync_at"] if present
    bq_sync_age_hours = 999.0
    if bq_connector and isinstance(bq_connector.metadata_json, dict):
        last_sync = bq_connector.metadata_json.get("last_sync_at")
        if last_sync:
            try:
                ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                bq_sync_age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            except (ValueError, AttributeError):
                pass

    def _has(src: str) -> bool:
        return has_netsuite if src == "netsuite" else has_bigquery

    def _healthy(src: str) -> bool:
        if src == "netsuite":
            return not netsuite_unhealthy
        return True  # BigQuery has no alert pipeline yet — trust sync-age guard below

    def _bq_age() -> float:
        return bq_sync_age_hours

    return {
        "tenant_has_connector": _has,
        "connector_is_healthy": _healthy,
        "bigquery_sync_age_hours": _bq_age,
    }
