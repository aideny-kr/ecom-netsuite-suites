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


# BUILTIN.DF(<alias>.country) used as a FILTER predicate — a per-row function in a
# filter defeats the index → full scan → 60s timeout (2026-06 ship-to-country
# incident). Matched in ANY clause (WHERE / JOIN-ON / HAVING) by requiring a
# comparison/IN/LIKE right after the closing paren, so it cannot escape via ON/HAVING.
# Display use — BUILTIN.DF(sa.country) AS country, GROUP BY BUILTIN.DF(sa.country) — has
# no operator after ')' and is NOT matched. Scoped to .country on purpose:
# BUILTIN.DF(field) = 'Value' on small static custom lists is a blessed readability
# pattern (netsuite.yaml CUSTOM LIST FIELDS) and must not be flagged. `BUILTIN\s*\.\s*DF`
# also catches a spaced-out `BUILTIN . DF` evasion.
_BUILTIN_DF_COUNTRY_FILTER = re.compile(
    r"BUILTIN\s*\.\s*DF\s*\(\s*\w+\.COUNTRY\s*\)\s*(?:=|<>|!=|>=|<=|>|<|\bIN\b|\bLIKE\b)"
)

# A real trandate predicate bounds the scan to a date range (uses the trandate index).
# Allow an optional ')' so both `t.trandate >= ...` and `TRUNC(t.trandate) >= ...` count;
# a SELECT/ORDER BY mention (trandate followed by ',' or end) does NOT (no operator).
# FETCH FIRST / ROWNUM are deliberately NOT bounds — they cap returned rows, not the scan.
_TRANDATE_PREDICATE = re.compile(r"\bTRANDATE\s*\)?\s*(?:>=|<=|>|<|=|\bBETWEEN\b)")

_ADDRESS_TABLES = ("TRANSACTIONSHIPPINGADDRESS", "TRANSACTIONBILLINGADDRESS")

# Penalty weight for each perf anti-pattern (subtracted from the efficiency score).
_PERF_PENALTY = {
    "builtin_df_country_filter": 0.3,
    "unbounded_address_join": 0.2,
}


def detect_perf_anti_patterns(sql: str) -> list[str]:
    """Return the proven perf anti-patterns present in *sql* (empty == clean).

    Both patterns are confirmed to time out (60s) on large NetSuite accounts
    (2026-06 ship-to-country incident):

      - ``builtin_df_country_filter`` — ``BUILTIN.DF(<addr>.country)`` used as a
        filter predicate (per-row function → defeats the index → full scan).
      - ``unbounded_address_join`` — a ``transactionShippingAddress`` /
        ``transactionBillingAddress`` join with no ``t.trandate`` predicate to
        bound the scan.

    Intentionally narrow: a generic ``BUILTIN.DF(field) = 'Value'`` filter on a
    small static custom list is a blessed readability pattern and is NOT flagged.
    """
    if not sql or not sql.strip():
        return []
    sql_upper = sql.upper()
    reasons: list[str] = []
    if _BUILTIN_DF_COUNTRY_FILTER.search(sql_upper):
        reasons.append("builtin_df_country_filter")
    if any(tbl in sql_upper for tbl in _ADDRESS_TABLES) and not _TRANDATE_PREDICATE.search(sql_upper):
        reasons.append("unbounded_address_join")
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
