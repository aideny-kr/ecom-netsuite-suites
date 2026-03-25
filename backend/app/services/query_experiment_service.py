"""Query experiment service — generate SQL, execute, score, decide KEEP/REVERT.

Runs a single experiment for the autonomous query improvement loop:
  1. Generate SQL via Haiku (~$0.03/call)
  2. Execute against live NetSuite (SuiteQL) or BigQuery
  3. Score with the eval harness (accuracy, syntax, efficiency)
  4. Decide: KEEP if experiment_score > baseline + 0.05, else REVERT

Budget: ~$0.15/experiment for SuiteQL, ~$0.20 for BigQuery.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.models.experiment_log import ExperimentLog
from app.models.mcp_connector import McpConnector
from app.services.query_eval_harness import (
    EvalCase,
    composite_score,
    score_accuracy,
    score_efficiency,
    score_syntax,
)
from app.services.query_pattern_service import extract_and_store_pattern

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_KEEP_THRESHOLD = 0.05  # experiment must beat baseline by this much
_BIGQUERY_MAX_COST_USD = 0.20  # skip BigQuery if dry-run exceeds this

_SUITEQL_DIALECT_RULES = """
SuiteQL dialect rules:
- Use FETCH FIRST N ROWS ONLY (not LIMIT)
- Use TRUNC(SYSDATE) instead of CURRENT_DATE
- Use BUILTIN.DF() for display values
- Status codes are single letters like 'B', 'H' (not compound like 'SalesOrd:B')
- JOIN syntax is standard SQL
- String concatenation uses ||
"""

_BIGQUERY_DIALECT_RULES = """
BigQuery Standard SQL rules:
- Use LIMIT N (not FETCH FIRST)
- Use backtick-quoted table references: `project.dataset.table`
- Use DATE/TIMESTAMP functions (CURRENT_DATE(), DATE_TRUNC, etc.)
- Use STRUCT and ARRAY types where appropriate
- Use SAFE_DIVIDE() to avoid division by zero
"""


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_experiment_cost(dialect: str) -> float:
    """Return estimated USD cost for one experiment.

    SuiteQL: ~$0.03 LLM + $0 execution (REST API) ≈ $0.15 with overhead.
    BigQuery: ~$0.03 LLM + $0.02 dry-run + $0.05 execution ≈ $0.20.
    """
    if dialect == "bigquery":
        return 0.20
    return 0.15


# ---------------------------------------------------------------------------
# SQL generation via Haiku
# ---------------------------------------------------------------------------


async def _call_haiku(system: str, user_msg: str) -> str:
    """Call Claude Haiku and return the text response."""
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


async def _generate_sql(
    question: str,
    dialect: str,
    schema_hint: str = "",
) -> str | None:
    """Generate SQL from a natural-language question using Haiku.

    Returns the SQL string on success, None on failure.
    """
    dialect_rules = _SUITEQL_DIALECT_RULES if dialect == "suiteql" else _BIGQUERY_DIALECT_RULES

    system = (
        f"You are a SQL expert. Generate a single read-only {dialect.upper()} query "
        f"that answers the user's question. Return ONLY the SQL — no explanation, "
        f"no markdown fences.\n\n{dialect_rules}"
    )
    if schema_hint:
        system += f"\n\nAvailable schema:\n{schema_hint}"

    try:
        sql = await _call_haiku(system, question)
        # Strip markdown fences if the model wraps them anyway
        if sql.startswith("```"):
            lines = sql.split("\n")
            sql = "\n".join(line for line in lines if not line.startswith("```")).strip()
        return sql or None
    except Exception:
        logger.exception("SQL generation failed for question=%s dialect=%s", question, dialect)
        return None


# ---------------------------------------------------------------------------
# SQL execution
# ---------------------------------------------------------------------------


async def _execute_suiteql(
    sql: str,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> dict[str, Any]:
    """Execute a SuiteQL query against the tenant's NetSuite connection."""
    from app.services.netsuite_client import execute_suiteql
    from app.services.netsuite_oauth_service import get_valid_token

    # Look up active NetSuite connection
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
            "success": False,
            "error": "No active NetSuite connection",
            "result_text": "",
            "rows": 0,
            "bytes_processed": 0,
        }

    # Decrypt and get token
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
    except Exception as exc:
        return {"success": False, "error": f"Decrypt failed: {exc}", "result_text": "", "rows": 0, "bytes_processed": 0}

    access_token = await get_valid_token(db, connection)
    if not access_token:
        return {"success": False, "error": "OAuth token expired", "result_text": "", "rows": 0, "bytes_processed": 0}

    account_id = credentials["account_id"].replace("_", "-").lower()

    try:
        raw = await execute_suiteql(
            access_token,
            account_id,
            sql,
            limit=100,
            paginate=False,
            timeout_seconds=30,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc), "result_text": "", "rows": 0, "bytes_processed": 0}

    # Build result text from returned data
    items = raw.get("items", [])
    row_count = raw.get("totalResults", len(items))
    result_text = " ".join(str(item) for item in items[:20])

    return {
        "success": True,
        "result_text": result_text,
        "rows": row_count,
        "bytes_processed": 0,
    }


async def _execute_bigquery(
    sql: str,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> dict[str, Any]:
    """Execute a BigQuery query against the tenant's BigQuery connector."""
    from app.services.bigquery_service import estimate_query_cost, execute_query

    # Look up active BigQuery connector
    result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "bigquery",
            McpConnector.status == "active",
        )
    )
    connector = result.scalars().first()
    if not connector:
        return {
            "success": False,
            "error": "No active BigQuery connector",
            "result_text": "",
            "rows": 0,
            "bytes_processed": 0,
        }

    try:
        creds = decrypt_credentials(connector.encrypted_credentials)
    except Exception as exc:
        return {"success": False, "error": f"Decrypt failed: {exc}", "result_text": "", "rows": 0, "bytes_processed": 0}

    project_id = creds.get("project_id", "")
    service_creds = creds.get("service_account", creds)

    # Dry-run cost check
    try:
        cost_info = await estimate_query_cost(
            credentials=service_creds,
            project_id=project_id,
            query=sql,
        )
        if cost_info.get("estimated_cost_usd", 0) > _BIGQUERY_MAX_COST_USD:
            return {
                "success": False,
                "error": (
                    f"Estimated cost ${cost_info['estimated_cost_usd']:.4f} exceeds ${_BIGQUERY_MAX_COST_USD} limit"
                ),
                "result_text": "",
                "rows": 0,
                "bytes_processed": cost_info.get("estimated_bytes", 0),
            }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Cost estimation failed: {exc}",
            "result_text": "",
            "rows": 0,
            "bytes_processed": 0,
        }

    # Execute
    try:
        raw = await execute_query(
            credentials=service_creds,
            project_id=project_id,
            query=sql,
            max_rows=100,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc), "result_text": "", "rows": 0, "bytes_processed": 0}

    # Build result text
    columns = raw.get("columns", [])
    rows = raw.get("rows", [])
    row_count = raw.get("row_count", len(rows))
    bytes_processed = raw.get("bytes_processed", 0)

    # Create a readable text representation for accuracy scoring
    header = " | ".join(columns)
    data_lines = [" | ".join(str(v) for v in row) for row in rows[:20]]
    result_text = f"{header}\n" + "\n".join(data_lines)

    return {
        "success": True,
        "result_text": result_text,
        "rows": row_count,
        "bytes_processed": bytes_processed,
    }


async def _execute_sql(
    sql: str,
    dialect: str,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> dict[str, Any]:
    """Route SQL execution to the correct backend."""
    if dialect == "suiteql":
        return await _execute_suiteql(sql, tenant_id, db)
    elif dialect == "bigquery":
        return await _execute_bigquery(sql, tenant_id, db)
    else:
        return {
            "success": False,
            "error": f"Unknown dialect: {dialect}",
            "result_text": "",
            "rows": 0,
            "bytes_processed": 0,
        }


# ---------------------------------------------------------------------------
# Main experiment runner
# ---------------------------------------------------------------------------


async def run_single_experiment(
    *,
    case: EvalCase,
    tenant_id: uuid.UUID,
    db: AsyncSession,
    baseline_score: float = 0.0,
    schema_hint: str = "",
) -> dict[str, Any]:
    """Run a single experiment: generate → execute → score → decide.

    Returns a dict with keys:
        dialect, question, generated_sql, executed_successfully,
        score_accuracy, score_syntax, score_efficiency, experiment_score,
        baseline_score, delta, decision, error_message, cost_usd
    """
    result: dict[str, Any] = {
        "dialect": case.dialect,
        "question": case.question,
        "generated_sql": None,
        "executed_successfully": False,
        "score_accuracy": 0.0,
        "score_syntax": 0.0,
        "score_efficiency": 0.0,
        "experiment_score": 0.0,
        "baseline_score": baseline_score,
        "delta": 0.0,
        "decision": "SKIP",
        "error_message": None,
        "cost_usd": estimate_experiment_cost(case.dialect),
    }

    # Step 1: Generate SQL
    generated_sql = await _generate_sql(
        question=case.question,
        dialect=case.dialect,
        schema_hint=schema_hint,
    )
    if not generated_sql:
        result["error_message"] = "SQL generation failed"
        return result

    result["generated_sql"] = generated_sql

    # Step 2: Execute SQL
    exec_result = await _execute_sql(
        sql=generated_sql,
        dialect=case.dialect,
        tenant_id=tenant_id,
        db=db,
    )

    if not exec_result["success"]:
        result["error_message"] = exec_result.get("error", "Execution failed")
        return result

    result["executed_successfully"] = True

    # Step 3: Score
    acc = score_accuracy(exec_result["result_text"], case.expected_keywords)
    syn = score_syntax(generated_sql, case.dialect)
    eff = score_efficiency(
        generated_sql,
        rows_returned=exec_result.get("rows"),
        bytes_processed=exec_result.get("bytes_processed"),
    )
    exp_score = composite_score(acc, syn, eff)

    result["score_accuracy"] = acc
    result["score_syntax"] = syn
    result["score_efficiency"] = eff
    result["experiment_score"] = exp_score
    result["delta"] = round(exp_score - baseline_score, 4)

    # Step 4: Decide
    if exp_score > baseline_score + _KEEP_THRESHOLD:
        result["decision"] = "KEEP"
    else:
        result["decision"] = "REVERT"

    # Step 5: Promote KEEP results to proven patterns + log all experiments
    # Use test_query key expected by promote_experiment_result
    promote_result = dict(result)
    promote_result["test_query"] = case.question
    await promote_experiment_result(promote_result, tenant_id, db)

    return result


# ---------------------------------------------------------------------------
# Promote experiment results to proven patterns
# ---------------------------------------------------------------------------


async def promote_experiment_result(
    result: dict,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """Promote KEEP experiments to proven patterns and log all experiments."""

    # Store in experiment log regardless of decision
    log_entry = ExperimentLog(
        tenant_id=tenant_id,
        dialect=result.get("dialect", ""),
        hypothesis=result.get("hypothesis", "Auto-generated experiment"),
        test_query=result.get("test_query", ""),
        generated_sql=result.get("generated_sql"),
        executed_successfully=result.get("executed_successfully"),
        score_accuracy=result.get("score_accuracy"),
        score_syntax=result.get("score_syntax"),
        score_efficiency=result.get("score_efficiency"),
        experiment_score=result.get("experiment_score"),
        baseline_score=result.get("baseline_score"),
        delta=result.get("delta"),
        decision=result.get("decision"),
        error_message=result.get("error_message"),
        cost_usd=result.get("cost_usd"),
    )
    db.add(log_entry)

    # Only promote KEEP decisions to proven patterns
    if result.get("decision") != "KEEP":
        return

    sql = result.get("generated_sql", "")
    question = result.get("test_query", "")
    dialect = result.get("dialect", "suiteql")

    if not sql or not question:
        return

    # Build a tool_calls_log that extract_and_store_pattern expects
    tool_name = "netsuite_suiteql" if dialect == "suiteql" else "bigquery_sql"
    tool_calls_log = [
        {
            "tool": tool_name,
            "params": {"query": sql},
            "result": {"success": True, "row_count": 1},  # Minimal success indicator
        }
    ]

    await extract_and_store_pattern(db, tenant_id, question, tool_calls_log)
    print(f"[AUTO_IMPROVE] Promoted pattern: {question[:60]}", flush=True)
