"""Query evaluation harness — scoring functions and test case loader.

Scores generated SQL queries on four dimensions:
  - syntax:     dialect correctness (SuiteQL vs BigQuery rules)
  - accuracy:   keyword hit ratio against expected answer tokens
  - efficiency: query structure quality (avoids SELECT *, uses CTEs/GROUP BY)
  - sql_match:  fragment hit ratio of expected SQL constructs in generated SQL

Composite weight: accuracy 0.30, syntax 0.30, efficiency 0.15, sql_match 0.25.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_EVAL_DIR = Path(__file__).resolve().parent.parent.parent / "eval"

# Destructive SQL commands that should never appear in read-only agents.
_MUTATING_KEYWORDS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|MERGE)\b",
    re.IGNORECASE,
)


@dataclass
class EvalCase:
    question: str
    dialect: str
    expected_keywords: list[str]
    expected_sql_contains: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    difficulty: str = "medium"


@dataclass
class EvalScore:
    accuracy: float
    syntax: float
    efficiency: float
    composite: float


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def score_syntax(sql: str, dialect: str) -> float:
    """Return [0, 1] score for dialect-correctness of *sql*.

    Rules applied:
      SuiteQL:
        - Must start with SELECT (no mutations) → 0.0 if violated
        - LIMIT keyword → penalty (use FETCH FIRST N ROWS ONLY)
        - CURRENT_DATE / CURRENT_TIMESTAMP → penalty (use TRUNC(SYSDATE))
        - BUILTIN.* is a SuiteQL-only function → no penalty

      BigQuery:
        - Must start with SELECT (no mutations) → 0.0 if violated
        - FETCH FIRST … ROWS ONLY → penalty (use LIMIT)
        - BUILTIN.* → penalty (NetSuite-only)
    """
    if not sql or not sql.strip():
        return 0.0

    # Reject mutations immediately.
    if _MUTATING_KEYWORDS.match(sql):
        return 0.0

    sql_upper = sql.upper()
    score = 1.0

    if dialect == "suiteql":
        # LIMIT is a MySQL/BigQuery construct — wrong for SuiteQL.
        if re.search(r"\bLIMIT\b", sql_upper):
            score -= 0.3
        # CURRENT_DATE / CURRENT_TIMESTAMP — use TRUNC(SYSDATE) in SuiteQL.
        if re.search(r"\bCURRENT_DATE\b|\bCURRENT_TIMESTAMP\b", sql_upper):
            score -= 0.2

    elif dialect == "bigquery":
        # FETCH FIRST … ROWS ONLY is SuiteQL syntax, not BigQuery.
        if re.search(r"\bFETCH\s+FIRST\b", sql_upper):
            score -= 0.4
        # BUILTIN.DF and friends are NetSuite/SuiteQL-specific.
        if re.search(r"\bBUILTIN\s*\.", sql_upper):
            score -= 0.3

    return max(0.0, round(score, 4))


def score_accuracy(result_text: str, expected_keywords: list[str]) -> float:
    """Return hit ratio of *expected_keywords* found in *result_text*.

    Case-insensitive substring search.  Returns 0.0 when either argument is
    empty.
    """
    if not result_text or not expected_keywords:
        return 0.0

    lower = result_text.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in lower)
    return round(hits / len(expected_keywords), 4)


# BUILTIN.DF(<addr>.country) used as a FILTER predicate — a per-row function in a filter
# defeats the index → full scan → 60s timeout (2026-06 ship-to-country incident). The
# country arg is anchored on the '(' of the DF call (with optional `<alias>.`) so COUNTRY
# cannot match as a suffix of another identifier (e.g. shipcountry). A comparison /
# (NOT) IN / (NOT) LIKE adjacent on EITHER side marks a filter — matched in ANY clause
# (WHERE / JOIN-ON / HAVING) and for reversed forms (`'SG' = BUILTIN.DF(sa.country)`).
# Display use — BUILTIN.DF(sa.country) AS country, GROUP BY BUILTIN.DF(sa.country) — has
# no adjacent operator and is NOT matched. Scoped to .country on purpose:
# BUILTIN.DF(field) = 'Value' on small static custom lists is a blessed readability
# pattern (netsuite.yaml CUSTOM LIST FIELDS) and must not be flagged by this check (an
# unbounded transactionline scan is caught by _BUILTIN_DF_FILTER below). `BUILTIN\s*\.\s*DF`
# also catches a spaced-out `BUILTIN . DF` evasion.
_DF_COUNTRY = r"BUILTIN\s*\.\s*DF\s*\(\s*(?:\w+\.)?COUNTRY\s*\)"
_CMP = r"(?:>=|<=|<>|!=|>|<|=|(?:NOT\s+)?IN\b|(?:NOT\s+)?LIKE\b)"
# Tolerate single-level wrappers around the country DF: a closing wrapper paren before
# the operator (`LOWER(BUILTIN.DF(sa.country)) = ...`) and a wrapper function-open after
# the operator (`... = LOWER(BUILTIN.DF(sa.country))`). Documented residual: multi-arg
# wrappers (`NVL(BUILTIN.DF(sa.country), '')`) still evade this static check — the live
# latency gate (follow-up #2) is the backstop for arbitrarily-shaped slow SQL.
_BUILTIN_DF_COUNTRY_FILTER = re.compile(rf"{_DF_COUNTRY}\s*\)*\s*{_CMP}|{_CMP}\s*(?:[A-Z_]+\s*\(\s*)*{_DF_COUNTRY}")

# A scan is bounded by a date range with a LOWER limit (the trandate index then reads a slice):
# `>=`, `>`, `=` (one day) or BETWEEN, with an optional ')' for `TRUNC(t.trandate)`, or the
# reversed forms `X <= t.trandate` / `X < t.trandate` / `X = t.trandate`. An upper limit alone
# (`t.trandate <= today`) still reads all history: vs-MCP 2026-10-06, sales_country_canonical
# read "as of today" that way and timed out on both sides. `<>` / `!=` are not ranges, and
# FETCH FIRST / ROWNUM cap returned rows, not the scan. Alias-blind on purpose: a query that
# joins a transaction table has one date that matters.
_TRANDATE_PREDICATE = re.compile(
    r"\bTRANDATE\s*\)?\s*(?:>=|>|=|\bBETWEEN\b)"
    r"|(?:<=|<(?![>=])|(?<![<>!])=)\s*(?:TRUNC\s*\(\s*)?(?:\w+\s*\.\s*)?TRANDATE\b"
)

_ADDRESS_TABLES = ("TRANSACTIONSHIPPINGADDRESS", "TRANSACTIONBILLINGADDRESS")

# BUILTIN.DF(<any field>) used as a filter, same operator/wrapper shapes as the country check.
# A two-argument wrapper (`NVL(BUILTIN.DF(x), 'NONE') = ...`) is a filter too (#390 review R3).
_DF_ANY = r"BUILTIN\s*\.\s*DF\s*\(\s*[\w.]+\s*\)"
_DEFAULT_ARG = r"(?:\s*,\s*(?:'S'|[\w.]+))?"  # literals are 'S' after _lex
_BUILTIN_DF_FILTER = re.compile(rf"{_DF_ANY}{_DEFAULT_ARG}\s*\)*\s*{_CMP}|{_CMP}\s*(?:[A-Z_]+\s*\(\s*)*{_DF_ANY}")
_TRANSACTION_LINES = re.compile(r"\bTRANSACTIONLINE\b")
_CASE_TOKEN = re.compile(r"\b(CASE|END)\b")
_PREDICATE_BEFORE = re.compile(r"\b(?:WHERE|AND|OR|ON|HAVING|NOT)\s*\(*\s*$")
_COMPARED_AFTER = re.compile(rf"\s*\)*\s*{_CMP}")
_NOT_AN_ALIAS = {"WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "ON", "GROUP", "ORDER", "FETCH", "UNION"}


def _lex(sql: str) -> str:
    """Upper-cased SQL with every string literal replaced by 'S' (handling '' escapes) and
    comments removed outside literals, so a value can't fake or hide syntax (#390 review R3/R5)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql[i] == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and not sql.startswith("''", j)):
                j += 2 if sql.startswith("''", j) else 1
            out.append("'S'")
            i = j + 1
        elif sql[i] == '"':
            j = sql.find('"', i + 1)
            j = n if j < 0 else j
            out.append(sql[i : j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(sql[i])
            i += 1
    return "".join(out).upper()


def _without_case(text: str) -> str:
    """*text* minus every balanced CASE ... END block used as a VALUE (nested ones included): a
    display label compares but filters nothing, and its dates bound nothing (#390 review R1/R4).
    A CASE used as a predicate stays: one that follows WHERE/AND/OR/ON/HAVING/NOT, or is itself
    compared after its END (#397 review R4). Unbalanced SQL is kept whole, so a malformed query
    can only be over-checked, never under-checked."""
    spans, depth, begin = [], 0, 0
    for m in _CASE_TOKEN.finditer(text):
        if m.group(1) == "CASE":
            if depth == 0:
                begin = m.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                spans.append((begin, m.end()))
    if depth:
        return text
    kept, last = [], 0
    for begin, end in spans:
        predicate = _PREDICATE_BEFORE.search(text[:begin]) or _COMPARED_AFTER.match(text, end)
        if not predicate:
            kept.append(text[last:begin])
            kept.append(" ")
            last = end
    kept.append(text[last:])
    return "".join(kept)


def _aliases(text: str, table: str) -> set[str]:
    found = re.findall(rf"\b(?:FROM|JOIN)\s+{table}\s+(?:AS\s+)?(\w+)", text)
    return {a for a in found if a not in _NOT_AN_ALIAS}


def _top_level(text: str) -> str:
    """Only the characters outside every parenthesis."""
    kept, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            kept.append(ch)
    return "".join(kept)


def _is_transaction_lookup(text: str) -> bool:
    """Specific rows by key read a handful of rows, so they bound the scan like a date range:
    `t.id = 123`, `t.tranid IN ('SO1')`, `tl.transaction = 987`, or one address by its key
    (`sa.nKey = 123`, #397 review R2). The key must stand directly after WHERE or AND, and no
    OR may sit outside parentheses, so an ID in an OR branch does not exempt the rest (#397
    review R3); a parenthesized OR group such as `(t.custbody1 = 'F' OR ... IS NULL)` is fine."""
    if re.search(r"\bOR\b", _top_level(text)):
        return False  # with an OR outside parentheses, precedence decides which branch scans
    literal = r"(?:=\s*(?:\d+\b|'S')|IN\s*\(\s*(?:\d+|'S')[^)]*\))"
    keys = [(table, r"(?:ID|TRANID)") for table in ("TRANSACTION",)]
    keys += [("TRANSACTIONLINE", "TRANSACTION")]
    keys += [(table, "NKEY") for table in _ADDRESS_TABLES]
    for table, column in keys:
        for alias in _aliases(text, table):
            if re.search(rf"\b(?:WHERE|AND)\s+{alias}\s*\.\s*{column}\s*{literal}", text):
                return True
    return False


# Penalty weight for each perf anti-pattern (subtracted from the efficiency score).
_PERF_PENALTY = {
    "builtin_df_country_filter": 0.3,
    "unbounded_address_join": 0.2,
    "unbounded_df_line_scan": 0.3,
}


def detect_perf_anti_patterns(sql: str) -> list[str]:
    """Return the proven perf anti-patterns present in *sql* (empty == clean).

    Both patterns are confirmed to time out (60s) on large NetSuite accounts
    (2026-06 ship-to-country incident):

      - ``builtin_df_country_filter`` — ``BUILTIN.DF(<addr>.country)`` used as a
        filter predicate (per-row function → defeats the index → full scan).
      - ``unbounded_address_join`` — a ``transactionShippingAddress`` /
        ``transactionBillingAddress`` join with no lower ``t.trandate`` limit (an
        upper limit alone still reads all history: vs-MCP 2026-10-06, 54-60 s and a
        timeout on both sides) and no lookup of specific transactions.

    And one measured on Framework 2026-10-05 (a 7-minute chat turn):

      - ``unbounded_df_line_scan`` — any ``BUILTIN.DF(...)`` filter in a query that
        reads ``transactionline`` with no trandate range. Each such query took
        65-116 s and timed out on the MCP; with a trandate floor the same filter
        returned the same rows in seconds. An item subquery
        (``tl.item IN (SELECT ... WHERE BUILTIN.DF(...) = ...)``) timed out as well,
        so the subquery is not exempt.

    The SQL is lexed first (``_lex`` + ``_without_case``): string literals, comments
    and CASE labels can neither fake nor hide a filter or a range.

    A ``BUILTIN.DF(field) = 'Value'`` filter on a small static custom list is
    still a blessed readability pattern on its own table, or once the scan is
    bounded by date; only the unbounded transaction-line scan is flagged.
    """
    if not sql or not sql.strip():
        return []
    # Read the SQL first: literals, comments and CASE labels can neither fake nor hide a filter
    # or a range, and a commented-out predicate can't satisfy a scan bound.
    text = _without_case(_lex(sql))
    bounded = bool(_TRANDATE_PREDICATE.search(text)) or _is_transaction_lookup(text)
    reasons: list[str] = []
    if _BUILTIN_DF_COUNTRY_FILTER.search(text):
        reasons.append("builtin_df_country_filter")
    if any(tbl in text for tbl in _ADDRESS_TABLES) and not bounded:
        reasons.append("unbounded_address_join")
    if _TRANSACTION_LINES.search(text) and _BUILTIN_DF_FILTER.search(text) and not bounded:
        reasons.append("unbounded_df_line_scan")
    return reasons


def score_efficiency(sql: str) -> float:
    """Return [0, 1] efficiency score for *sql*.

    Heuristics (all applied to the SQL text):
      - SELECT *           → −0.2  (fetches unnecessary columns)
      - GROUP BY           → +0.05 bonus (aggregation pattern, usually intentional)
      - WITH (CTE)         → +0.05 bonus (structured, reusable sub-query)
      - perf anti-patterns → −0.3 / −0.2  (see ``detect_perf_anti_patterns``)

    The perf-anti-pattern penalties guard the 2026-06 ship-to-country timeout
    (BUILTIN.DF country filters and unbounded address joins). The same detector
    backs a hard promotion veto in ``query_experiment_service`` so a timeout-prone
    pattern can never be promoted even when its answer beats the baseline.
    """
    if not sql or not sql.strip():
        return 0.0

    sql_upper = sql.upper()
    score = 1.0

    # Penalise bare SELECT *.  We check for "SELECT *" or "SELECT\n*" but not
    # "SELECT COUNT(*)" which is intentional.
    if re.search(r"\bSELECT\s+\*", sql_upper) and not re.search(r"\bSELECT\s+COUNT\s*\(\s*\*\s*\)", sql_upper):
        score -= 0.2

    # Aggregation bonus.
    if re.search(r"\bGROUP\s+BY\b", sql_upper):
        score = min(1.0, score + 0.05)

    # CTE bonus.
    if re.search(r"\bWITH\s+\w", sql_upper):
        score = min(1.0, score + 0.05)

    # Proven perf anti-patterns (BUILTIN.DF country filter / unbounded address join).
    for reason in detect_perf_anti_patterns(sql):
        score -= _PERF_PENALTY[reason]

    return max(0.0, round(score, 4))


def score_sql_contains(sql: str, expected_fragments: list[str]) -> float:
    """Return hit ratio of expected SQL fragments found in generated SQL.

    Case-insensitive. Returns 0.0 when either argument is empty.
    """
    if not sql or not expected_fragments:
        return 0.0
    sql_upper = sql.upper()
    hits = sum(1 for frag in expected_fragments if frag.upper() in sql_upper)
    return round(hits / len(expected_fragments), 4)


def composite_score(accuracy: float, syntax: float, efficiency: float, sql_match: float = 0.0) -> float:
    """Weighted composite: accuracy 30%, syntax 30%, efficiency 15%, sql_match 25%."""
    return round(accuracy * 0.30 + syntax * 0.30 + efficiency * 0.15 + sql_match * 0.25, 4)


# ---------------------------------------------------------------------------
# Eval case loader
# ---------------------------------------------------------------------------


def load_eval_cases(dialect: str) -> list[EvalCase]:
    """Load eval cases from ``backend/eval/{dialect}_test_set.yaml``.

    Returns an empty list when the file does not exist or the dialect is
    unknown.
    """
    path = _EVAL_DIR / f"{dialect}_test_set.yaml"
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as fh:
        raw: list[dict[str, Any]] = yaml.safe_load(fh) or []

    cases: list[EvalCase] = []
    for item in raw:
        # Ensure the dialect field is always set from file-level context.
        item.setdefault("dialect", dialect)
        cases.append(
            EvalCase(
                question=item["question"],
                dialect=item.get("dialect", dialect),
                expected_keywords=item.get("expected_keywords", []),
                expected_sql_contains=item.get("expected_sql_contains", []),
                tables=item.get("tables", []),
                difficulty=item.get("difficulty", "medium"),
            )
        )

    return cases


async def load_db_eval_cases(
    db: "AsyncSession",  # noqa: F821
    tenant_id: "uuid.UUID",  # noqa: F821
    dialect: str,
) -> list[EvalCase]:
    """Load active eval cases from the database for a given tenant + dialect."""
    from sqlalchemy import select as sa_select

    from app.models.eval_case import EvalCase as EvalCaseModel

    stmt = (
        sa_select(EvalCaseModel)
        .where(
            EvalCaseModel.tenant_id == tenant_id,
            EvalCaseModel.dialect == dialect,
            EvalCaseModel.is_active == True,  # noqa: E712
        )
        .order_by(EvalCaseModel.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    return [
        EvalCase(
            question=row.question,
            dialect=row.dialect,
            expected_keywords=row.expected_keywords or [],
        )
        for row in rows
    ]
