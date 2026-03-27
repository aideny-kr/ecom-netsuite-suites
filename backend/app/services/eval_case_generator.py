"""Autonomous eval case generator — generates new test cases from schema hints.

Runs at the start of the nightly improvement loop. Uses Haiku to generate
novel questions from the schema, then validates they don't duplicate existing
cases. Adds 3-5 new cases per run (~$0.10/run).
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from typing import Any

import anthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.eval_case import EvalCase as EvalCaseModel
from app.services.eval_case_miner import _is_duplicate

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_MAX_GENERATED_PER_TENANT = 200

_SUITEQL_FOCUS_AREAS = [
    "status code filtering for different transaction types",
    "BUILTIN.DF() for display values on foreign key fields",
    "date range filtering with TRUNC(SYSDATE) or specific date ranges",
    "multi-table JOINs between transaction, transactionline, and entity tables",
    "aggregation with GROUP BY and HAVING",
    "window functions like LAG, RANK, running totals",
    "amount calculations (order-level t.foreigntotal vs line-level tl.foreignamount)",
]

_BIGQUERY_FOCUS_AREAS = [
    "customer segmentation using customer_type, customer_category, b2b_customer_name",
    "order-level vs line-level amounts (ecom_order_total vs netamount)",
    "time series analysis with DATE_TRUNC and window functions",
    "product analysis using itemclass, itemdisplayname, fw_sku",
    "geographic analysis using shipcountry, shipstate, billingcountry",
    "shipping and fulfillment using shipmethod, shippingcost, actualshipdate",
    "median/percentile using APPROX_QUANTILES",
]


async def _call_haiku(system: str, user_msg: str) -> str:
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


async def generate_eval_cases(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    dialect: str,
    max_new: int = 5,
) -> list[dict[str, Any]]:
    """Generate new eval cases from schema hints."""
    from app.services.query_experiment_service import (
        _BIGQUERY_SCHEMA_HINT,
        _SUITEQL_SCHEMA_HINT,
    )

    # Check cap
    count_result = await db.execute(
        select(func.count())
        .select_from(EvalCaseModel)
        .where(
            EvalCaseModel.tenant_id == tenant_id,
            EvalCaseModel.source == "generated",
        )
    )
    current_count = count_result.scalar() or 0
    if current_count >= _MAX_GENERATED_PER_TENANT:
        return []

    max_new = min(max_new, _MAX_GENERATED_PER_TENANT - current_count)

    schema_hint = _SUITEQL_SCHEMA_HINT if dialect == "suiteql" else _BIGQUERY_SCHEMA_HINT
    focus_areas = _SUITEQL_FOCUS_AREAS if dialect == "suiteql" else _BIGQUERY_FOCUS_AREAS
    focus = random.sample(focus_areas, min(2, len(focus_areas)))

    system = (
        f"You are a test case designer for a {dialect.upper()} query evaluation system. "
        f"Generate {max_new} novel business questions a finance team would ask. "
        f"For each, provide: question, expected_keywords (3-5 lowercase strings that MUST appear in correct results), "
        f"expected_sql_contains (3-5 SQL fragments that a correct query MUST contain). "
        f"Focus on questions that test: {', '.join(focus)}. "
        f"Return ONLY a JSON array. No explanation."
    )
    user_msg = f"Schema:\n{schema_hint}"

    try:
        raw = await _call_haiku(system, user_msg)
        # Strip markdown fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(line for line in lines if not line.startswith("```")).strip()
        cases_data = json.loads(raw)
    except Exception:
        logger.warning("eval_case_generator.haiku_failed", exc_info=True)
        return []

    if not isinstance(cases_data, list):
        return []

    # Load existing questions for dedup
    existing_result = await db.execute(
        select(EvalCaseModel.question).where(
            EvalCaseModel.tenant_id == tenant_id,
            EvalCaseModel.is_active == True,  # noqa: E712
        )
    )
    existing_questions = [row[0] for row in existing_result.all()]

    stored: list[dict[str, Any]] = []
    for item in cases_data[:max_new]:
        if not isinstance(item, dict):
            continue
        question = item.get("question", "").strip()
        if not question or len(question) < 10:
            continue
        if _is_duplicate(question, existing_questions):
            continue

        keywords = item.get("expected_keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k).lower().strip() for k in keywords if k][:8]

        sql_contains = item.get("expected_sql_contains", [])
        if not isinstance(sql_contains, list):
            sql_contains = []
        sql_contains = [str(s).strip() for s in sql_contains if s][:8]

        model = EvalCaseModel(
            tenant_id=tenant_id,
            question=question,
            dialect=dialect,
            expected_keywords=keywords,
            source="generated",
        )
        db.add(model)
        existing_questions.append(question)
        stored.append(
            {
                "question": question,
                "dialect": dialect,
                "expected_keywords": keywords,
                "expected_sql_contains": sql_contains,
            }
        )

    return stored
