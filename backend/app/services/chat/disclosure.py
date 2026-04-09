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
