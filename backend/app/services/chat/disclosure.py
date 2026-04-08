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
    current_source: str,
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

    other_source = "bigquery" if current_source == "netsuite" else "netsuite"

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
