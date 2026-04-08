"""Disclosure footer assembly — post-turn interpretation and filter extraction.

This module exposes:
- DisclosureBlock: the payload sent to the frontend as a `disclosure` SSE event.
- assemble_disclosure(): entry point called from the orchestrator after the
  final tool call has executed and before the final `message` event is yielded.
- compute_can_switch_source(): decides if the other data source is viable
  for this query class + tenant + connector health.
- classify_query_source_class(): maps a user query to a source-class label
  (orders / gl / marketing / saved_search / unknown).
- SOURCE_SWITCH_RE / PUSHBACK_RE: anchored regexes used by the chat endpoint.
- WHERE-clause parser helpers (internal).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Literal, Protocol


@dataclass
class DisclosureBlock:
    """Post-turn footer data sent to the frontend as a `disclosure` SSE event."""

    source: Literal["netsuite", "bigquery"]
    interpretation: str
    implicit_filters: list[str] = field(default_factory=list)
    can_switch_source: bool = False
    is_rerun: bool = False
    failure_mode: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ── Source-switch detection ───────────────────────────────────────────────


SOURCE_SWITCH_RE = re.compile(
    r"^\s*(?:use|switch\s+to|run\s+on|try)\s+(netsuite|bigquery|bq|ns)\s*[.!?]?\s*$",
    re.IGNORECASE,
)

_SOURCE_ALIASES = {
    "bq": "bigquery",
    "ns": "netsuite",
    "bigquery": "bigquery",
    "netsuite": "netsuite",
}


def parse_source_switch(message: str) -> str | None:
    """Return the target source ('netsuite' | 'bigquery') or None if not a switch command."""
    match = SOURCE_SWITCH_RE.match(message)
    if not match:
        return None
    return _SOURCE_ALIASES.get(match.group(1).lower())


# ── Pushback detection ────────────────────────────────────────────────────


PUSHBACK_RE = re.compile(
    r"^\s*(?:"
    r"that(?:'s|s|\s+is)?\s+(?:wrong|not\s+right)|"
    r"no[,\s]+i\s+meant|"
    r"actually\b|"
    r"why\s+is\b|"
    r"i\s+need\b"
    r")",
    re.IGNORECASE,
)


# ── Query source-class classification ────────────────────────────────────

from enum import Enum


class QueryClass(str, Enum):
    DUAL_SOURCE = "dual_source"
    NETSUITE_ONLY = "netsuite_only"
    BIGQUERY_ONLY = "bigquery_only"
    UNMATCHED = "unmatched"


# Order matters: NetSuite/BigQuery-only are checked before dual-source so
# "GL revenue by channel" routes to NetSuite-only, not dual-source.
_NETSUITE_ONLY_KEYWORDS = [
    "balance sheet", "income statement", "p&l", "pl statement",
    "gl ", " gl", "journal", "ledger", "period close", "close the period",
    "purchase order", "vendor bill",
    "saved search", "suitescript", "custom record", "custom field",
    "ar aging", "ap aging", "trial balance", "general ledger",
]

_BIGQUERY_ONLY_KEYWORDS = [
    "ad spend", "attribution", "campaign", "funnel", "conversion rate",
    "session", "cohort", "retention", "ctr", "cpc", "cpm", "roas",
]

_DUAL_SOURCE_KEYWORDS = [
    "order", "sale", "transaction", "customer", "item", "product",
    "revenue", "refund", "return", "invoice", "channel",
]


def classify_query_source_class(query: str) -> QueryClass:
    """Classify a user query into a source-availability class.

    Used to decide if the other data source (`can_switch_source`) is viable
    for this query. Conservative: unmatched queries return UNMATCHED, which
    disables the switch hint.
    """
    q = query.lower().strip()
    if not q:
        return QueryClass.UNMATCHED

    # Check single-source keywords FIRST so they beat dual-source overlaps
    for kw in _NETSUITE_ONLY_KEYWORDS:
        if kw in q:
            return QueryClass.NETSUITE_ONLY
    for kw in _BIGQUERY_ONLY_KEYWORDS:
        if kw in q:
            return QueryClass.BIGQUERY_ONLY
    for kw in _DUAL_SOURCE_KEYWORDS:
        if kw in q:
            return QueryClass.DUAL_SOURCE
    return QueryClass.UNMATCHED


# ── can_switch_source logic ──────────────────────────────────────────────


class ConnectorState(Protocol):
    """Protocol for the connector state the hook receives.

    Kept as a Protocol so tests can pass a lightweight fake and production
    can pass a real object assembled by the orchestrator from
    `connection_alerts` and the Stripe/BigQuery sync tables.
    """

    has_bigquery: bool
    has_netsuite: bool
    bq_healthy: bool
    ns_healthy: bool
    bq_sync_age: timedelta


_BIGQUERY_STALE_THRESHOLD = timedelta(hours=24)


def compute_can_switch_source(
    current_source: Literal["netsuite", "bigquery"],
    query: str,
    state: ConnectorState,
) -> bool:
    """Decide if the user should see a 'switch to X' hint in the disclosure footer.

    Returns True only if the other source has a healthy connector AND the
    query class is compatible with that source. Conservative: any ambiguity
    returns False.
    """
    query_class = classify_query_source_class(query)
    if query_class == QueryClass.UNMATCHED:
        return False

    if current_source == "netsuite":
        other_source = "bigquery"
    elif current_source == "bigquery":
        other_source = "netsuite"
    else:
        return False  # unknown source — conservative default

    if other_source == "bigquery":
        if query_class == QueryClass.NETSUITE_ONLY:
            return False
        if not state.has_bigquery or not state.bq_healthy:
            return False
        if state.bq_sync_age > _BIGQUERY_STALE_THRESHOLD:
            return False
        return True
    else:
        if query_class == QueryClass.BIGQUERY_ONLY:
            return False
        if not state.has_netsuite or not state.ns_healthy:
            return False
        return True


# ── WHERE-clause parser (best-effort, ~10 common shapes) ────────────────


@dataclass
class ParsedFilters:
    """Output of parse_where_clause: interpretation sentence + filter bullets."""

    interpretation: str = ""
    implicit_filters: list[str] = field(default_factory=list)


_TRANSACTION_TYPE_LABELS = {
    "SalesOrd": "Sales Order",
    "CustInvc": "Customer Invoice",
    "CustCred": "Credit Memo",
    "PurchOrd": "Purchase Order",
    "ItemShip": "Item Fulfillment",
    "CustPymt": "Customer Payment",
    "Journal": "Journal Entry",
    "Deposit": "Deposit",
    "RtnAuth": "Return Authorization",
}


def parse_where_clause(sql: str) -> ParsedFilters:
    """Parse a WHERE clause for common predicate shapes. Graceful degrade.

    Supported shapes:
    1. `trandate >= TRUNC(SYSDATE, 'WW' | 'MM' | 'Q' | 'YYYY')`
    2. `trandate >= 'YYYY-MM-DD' AND trandate <= 'YYYY-MM-DD'`
    3. `type = 'X'` / `type IN ('X', 'Y')`
    4. `status = 'X'` / `status IN ('X', 'Y')`
    5. `subsidiary = N`
    6. `is_test = false` / `is_test IS false`
    7. `cancelled_at IS NULL`
    8. `currency = N`
    9. `entity = N`

    Anything not matched is silently skipped.
    """
    if not sql:
        return ParsedFilters()

    # Strip block comments first (multi-line safe), then single-line comments.
    # NOTE: this naively strips `--` even inside SQL string literals like
    # `WHERE name = '-- not a comment'`. Acceptable for v0 since the failure
    # mode is an empty footer, not a wrong one.
    normalized = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    normalized = re.sub(r"--[^\n]*", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    where_match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bFETCH\b|$)",
        normalized,
        re.IGNORECASE,
    )
    if not where_match:
        return ParsedFilters()

    where_text = where_match.group(1).strip()
    if not where_text:
        return ParsedFilters()

    interpretation = ""
    filters: list[str] = []

    # 1. Relative date windows (check before literal ranges)
    rel_match = re.search(
        r"trandate\s*>=\s*TRUNC\s*\(\s*SYSDATE\s*,\s*'(WW|MM|Q|YYYY)'\s*\)",
        where_text,
        re.IGNORECASE,
    )
    if rel_match:
        grain = rel_match.group(1).upper()
        interpretation = {
            "WW": "This week (Monday–today)",
            "MM": "This month",
            "Q": "This quarter",
            "YYYY": "This year",
        }[grain]

    # 2. Literal date range
    if not interpretation:
        range_match = re.search(
            r"trandate\s*>=\s*'?(\d{4}-\d{2}-\d{2})'?\s+AND\s+trandate\s*<=\s*'?(\d{4}-\d{2}-\d{2})'?",
            where_text,
            re.IGNORECASE,
        )
        if range_match:
            interpretation = f"{range_match.group(1)} – {range_match.group(2)}"

    # 3. Transaction type
    type_in_match = re.search(r"\btype\s+IN\s*\(([^)]+)\)", where_text, re.IGNORECASE)
    if type_in_match:
        codes = [c.strip().strip("'\"") for c in type_in_match.group(1).split(",")]
        labels = [_TRANSACTION_TYPE_LABELS.get(c, c) for c in codes]
        filters.append(f"Transaction type: {', '.join(labels)}")
    else:
        type_eq_match = re.search(r"\btype\s*=\s*'([^']+)'", where_text, re.IGNORECASE)
        if type_eq_match:
            code = type_eq_match.group(1)
            label = _TRANSACTION_TYPE_LABELS.get(code, code)
            filters.append(f"Transaction type: {label}")

    # 4. Status predicates
    status_match = re.search(
        r"\bstatus\s+IN\s*\(([^)]+)\)|\bstatus\s*=\s*'([^']+)'",
        where_text,
        re.IGNORECASE,
    )
    if status_match:
        codes_raw = status_match.group(1) or status_match.group(2)
        codes = [c.strip().strip("'\"") for c in codes_raw.split(",")]
        filters.append(f"Status: {', '.join(codes)}")

    # 5. Subsidiary
    sub_match = re.search(r"\bsubsidiary\s*=\s*(\d+)", where_text, re.IGNORECASE)
    if sub_match:
        filters.append(f"Subsidiary ID {sub_match.group(1)}")

    # 6. is_test flag
    if re.search(r"\bis_test\s*=\s*false\b", where_text, re.IGNORECASE):
        filters.append("Excludes test orders")

    # 7. cancelled_at IS NULL
    if re.search(r"\bcancelled_at\s+IS\s+NULL\b", where_text, re.IGNORECASE):
        filters.append("Excludes cancelled orders")

    # 8. Currency
    curr_match = re.search(r"\bcurrency\s*=\s*(\d+)", where_text, re.IGNORECASE)
    if curr_match:
        filters.append(f"Currency ID {curr_match.group(1)}")

    # 9. Entity
    entity_match = re.search(r"\bentity\s*=\s*(\d+)", where_text, re.IGNORECASE)
    if entity_match:
        filters.append(f"Entity ID {entity_match.group(1)}")

    return ParsedFilters(interpretation=interpretation, implicit_filters=filters)


# ── Entry point ──────────────────────────────────────────────────────────

SOURCE_LABELS = {"netsuite": "NetSuite", "bigquery": "BigQuery"}

_DATA_TOOL_NAMES = {
    "netsuite_suiteql",
    "bigquery_sql",
    "netsuite_financial_report",
    "pivot_query_result",
    "ns_runCustomSuiteQL",
    "ns_getRecord",
    "netsuite_saved_search",
    "ns_runReport",
    "ns_runSavedSearch",
}

_STALE_PATTERN_THRESHOLD_DAYS = 7


def _data_tool_key(name: str) -> str:
    """Strip the `ext__{32-hex}__` prefix used by external MCP tool names.

    External MCP tools are exposed to the LLM with a uniqueness prefix
    (see `app/services/chat/tools.py`); the disclosure footer needs to
    match against the underlying raw tool name.
    """
    if name.startswith("ext__"):
        # ext__{32 hex chars}__{raw_name}
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def assemble_disclosure(
    *,
    tool_calls: list[dict],
    user_query: str,
    current_source: Literal["netsuite", "bigquery"],
    connector_state: ConnectorState,
    matched_pattern: dict | None,
    is_rerun: bool,
) -> DisclosureBlock | None:
    """Assemble a DisclosureBlock for the current turn, or None if not applicable.

    Rules:
    1. No data-returning tool called → None
    2. Proven pattern matched AND age < 7 days → None
    3. All tool calls failed → failure-mode footer (only if can_switch_source)
    4. Otherwise → parse the LAST successful data tool's SQL
    """
    data_tool_calls = [t for t in tool_calls if _data_tool_key(t.get("tool", "")) in _DATA_TOOL_NAMES]
    if not data_tool_calls:
        return None

    # Rule 2: fresh pattern suppresses the footer
    if matched_pattern and matched_pattern.get("age_days", 0) < _STALE_PATTERN_THRESHOLD_DAYS:
        return None

    successful = [t for t in data_tool_calls if t.get("success", True)]
    failed = [t for t in data_tool_calls if not t.get("success", True)]

    can_switch = compute_can_switch_source(current_source, user_query, connector_state)

    # Rule 3: failure mode
    if not successful and failed:
        if not can_switch:
            return None
        return DisclosureBlock(
            source=current_source,
            interpretation=f"Tried {SOURCE_LABELS[current_source]}.",
            implicit_filters=[],
            can_switch_source=True,
            is_rerun=is_rerun,
            failure_mode=True,
        )

    # Rule 4: parse the last successful call
    primary = successful[-1]
    sql = primary.get("params", {}).get("query", "")
    parsed = parse_where_clause(sql)

    return DisclosureBlock(
        source=current_source,
        interpretation=parsed.interpretation,
        implicit_filters=parsed.implicit_filters,
        can_switch_source=can_switch,
        is_rerun=is_rerun,
        failure_mode=False,
    )
