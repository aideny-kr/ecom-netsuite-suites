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


def score_efficiency(
    sql: str,
    rows_returned: int | None = None,
    bytes_processed: int | None = None,
) -> float:
    """Return [0, 1] efficiency score for *sql*.

    Heuristics (all applied to the SQL text):
      - SELECT *       → −0.2  (fetches unnecessary columns)
      - GROUP BY       → +0.05 bonus (aggregation pattern, usually intentional)
      - WITH (CTE)     → +0.05 bonus (structured, reusable sub-query)

    Future: rows_returned and bytes_processed can gate additional penalties
    when runtime data is available.
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
    db: "AsyncSession",
    tenant_id: "uuid.UUID",
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
