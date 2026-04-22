"""Query experiment service — generate SQL, execute, score, decide KEEP/REVERT.

Runs a single experiment for the autonomous query improvement loop:
  1. Generate candidate SQL via Haiku (~$0.03/call)
  2. Execute against live NetSuite (SuiteQL) or BigQuery
  3. Run both our agent AND Claude+MCP baseline on the same question
  4. Score both answers with substring_score (fast, deterministic)
  5. Decide:
       KEEP   — agent_score >= baseline_score AND agent_score > 0.5
       REVERT — agent_score < baseline_score - 0.1 (we got worse)
       SKIP   — neither better nor worse

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

# vs-MCP benchmark scoring — replaces the internal composite scorer.
# Imported here so callers (and tests) can patch them in this module's namespace.
from app.services.benchmarks.agent_runner import run_agent  # noqa: F401
from app.services.benchmarks.baseline_runner import run_baseline  # noqa: F401
from app.services.benchmarks.scorer import substring_score  # noqa: F401
from app.services.query_eval_harness import (
    EvalCase,
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
- Use BUILTIN.DF() for display values of list/record fields
- Status codes are single letters like 'B', 'H' (not compound like 'SalesOrd:B')
- JOIN syntax is standard SQL
- String concatenation uses ||
- Primary key column is "id" (NOT "internalid")
- For latest records, use ORDER BY t.id DESC (more reliable than dates)
"""

_SUITEQL_SCHEMA_HINT = """
Core tables and key columns:

transaction (alias: t)
  id, tranid, trandate, type, status, entity, subsidiary, department,
  class, location, memo, foreigntotal, total, currency, exchangerate, createddate, duedate
  Types: SalesOrd, CustInvc, VendBill, PurchOrd, RtnAuth, CashSale, CustPymt, Journal, TrnfrOrd, ItemShip, ItemRcpt, WorkOrd
  AMOUNT RULES:
  - t.foreigntotal = amount in TRANSACTION currency
  - t.total = amount in SUBSIDIARY BASE currency (usually USD)
  - For order-level totals WITHOUT transactionline join: use t.foreigntotal or t.total
  - If query has JOIN transactionline, you MUST use line-level amounts (tl.foreignamount, tl.netamount), NOT t.foreigntotal

transactionline (alias: tl) — JOIN ON tl.transaction = t.id
  id, transaction, item, quantity, netamount, foreignamount, rate, amount,
  department, class, location, subsidiary
  - tl.foreignamount = line amount in TRANSACTION currency
  - tl.netamount = line net amount
  - Revenue lines are NEGATIVE — use ABS(tl.netamount) or * -1

customer (alias: c)
  id, entityid, companyname, email, phone, subsidiary, category,
  datecreated, lastmodifieddate, isinactive

item (alias: i)
  id, itemid, displayname, type, class, department, baseprice,
  averagecost, quantityavailable, quantityonhand, reorderpoint, isinactive

account (alias: a)
  id, acctnumber, acctname, accttype, balance

employee (alias: emp)
  id, entityid, firstname, lastname, email, department, subsidiary, isinactive

subsidiary (alias: s)
  id, name, isinactive

department (alias: d)
  id, name, isinactive

transactionaccountingline (alias: tal) — for GL/financial queries
  transaction, account, amount, debit, credit, subsidiary, posting

STATUS CODES — CRITICAL (single-letter only, NEVER compound like 'SalesOrd:B'):
  Sales Order (SalesOrd): A=Pending Approval, B=Pending Fulfillment, C=Cancelled, D=Partially Fulfilled, E=Pending Billing/Partially Fulfilled, F=Pending Billing, G=Billed, H=Closed
  Purchase Order (PurchOrd): A=Pending Supervisor Approval, B=Pending Receipt, C=Rejected, D=Partially Received, E=Pending Billing/Partially Received, F=Pending Bill, G=Fully Billed, H=Closed
  Return Authorization (RtnAuth): A=Pending Approval, B=Pending Receipt, C=Cancelled, D=Partially Received, E=Pending Refund/Partially Received, F=Pending Refund, G=Refunded, H=Closed
  Invoice (CustInvc): A=Open, B=Paid In Full
  Vendor Bill (VendBill): A=Open, B=Paid In Full
  Item Fulfillment (ItemShip): A=Shipped, B=Packed, C=Picked
  Active SOs: status NOT IN ('C', 'H')
  Open POs: status NOT IN ('G', 'H')
  RMAs received: status IN ('D', 'E', 'F', 'G', 'H')
  Overdue invoices: type = 'CustInvc' AND status = 'A' AND duedate < TRUNC(SYSDATE)

DISPLAY VALUES — use BUILTIN.DF() for foreign-key / list fields:
  BUILTIN.DF(t.entity) → customer/vendor name
  BUILTIN.DF(tl.item) → item display name
  BUILTIN.DF(t.subsidiary) → subsidiary name
  BUILTIN.DF(t.department) → department name
  BUILTIN.DF(t.status) → status display text
  BUILTIN.DF(t.currency) → currency name

Common JOINs:
  transaction t JOIN transactionline tl ON tl.transaction = t.id
  transactionline tl JOIN item i ON tl.item = i.id
  transaction t JOIN customer c ON t.entity = c.id
  transaction t JOIN subsidiary s ON t.subsidiary = s.id
  transactionline tl JOIN account a ON tl.account = a.id
  transactionaccountingline tal JOIN account a ON tal.account = a.id

DATE FUNCTIONS:
  Today: TRUNC(SYSDATE)
  Last N days: t.trandate >= TRUNC(SYSDATE) - N
  This month: t.trandate >= TRUNC(SYSDATE, 'MONTH')
  This quarter: t.trandate >= TRUNC(SYSDATE, 'Q')
  This year: t.trandate >= TRUNC(SYSDATE, 'YEAR')
  Specific date: TO_DATE('2026-01-01', 'YYYY-MM-DD')
  NEVER use CURRENT_DATE — not supported in SuiteQL

PAGINATION: Use FETCH FIRST N ROWS ONLY (never LIMIT)
"""

_BIGQUERY_DIALECT_RULES = """
BigQuery Standard SQL rules:
- Use LIMIT N (not FETCH FIRST)
- Use backtick-quoted table references: `project.dataset.table`
- Use DATE/TIMESTAMP functions (CURRENT_DATE(), DATE_TRUNC, etc.)
- Use STRUCT and ARRAY types where appropriate
- Use SAFE_DIVIDE() to avoid division by zero
"""

_BIGQUERY_SCHEMA_HINT = """
Dataset: `frameworkreporting`
Primary table: `frameworkreporting.sales-orders_cleaned` (partitioned by orderdate)

ALL COLUMNS — use ONLY these exact names:
  ordernumber (STRING) — unique order ID (e.g. "R296292279")
  orderdate (DATE) — partition key, always filter on this for performance
  orderstatus (STRING) — "Billed", "Pending Fulfillment", "Cancelled", "Closed", etc.
  solidusorderstatus (STRING) — ecommerce platform status
  item (STRING) — item SKU/code
  itemdisplayname (STRING) — human-readable item name
  itemclass (STRING) — product category: "Laptop", "Keyboard", "SSD", "RAM", "Expansion Card", etc.
  itemhierarchy (STRING) — product hierarchy path
  platform (STRING) — sales platform
  Refurbished (BOOLEAN) — note: capital R
  quantity (INTEGER) — line quantity
  quantityfulfilled_received (INTEGER) — fulfilled/received qty
  lastpurchaseprice (FLOAT) — cost price
  netamount (FLOAT) — line-level net revenue (NOT "net_amount")
  salesprice (FLOAT) — unit sale price
  actualshipdate (DATE) — ship date
  shipcountry (STRING) — ship-to country (e.g. "US", "CA", "DE")
  shipstate (STRING) — ship-to state
  batchlabel (STRING) — production batch
  ordermonth (STRING) — pre-computed order month string
  shipmonth (STRING) — pre-computed ship month string
  orderweeknumber (INTEGER) — ISO week number of order
  shipweeknumber (INTEGER) — ISO week number of shipment
  email (STRING) — customer email (use as customer identifier for cohorts/LTV)
  preorder (BOOLEAN) — preorder flag
  lineuniquekey (STRING) — unique line identifier
  order_type (STRING) — "Standard", "Replacement", etc.
  line_order_type (STRING) — line-level order type
  customer_type (STRING) — "B2C", "B2B"
  customer_category (STRING) — customer segment
  b2b_customer_name (STRING) — B2B company name (NULL for B2C)
  shipmethod (STRING) — shipping method
  shippingcost (FLOAT) — shipping cost
  billingcountry (STRING) — billing country
  date_closed (DATE) — order close date
  ecom_order_total (FLOAT) — ORDER-LEVEL total (use for order value analysis, NOT netamount which is per-line)
  location (STRING) — fulfillment location
  fw_sku (STRING) — Framework-specific SKU

CRITICAL RULES:
- Backticks required: `frameworkreporting.sales-orders_cleaned` (hyphen in name)
- For order-level totals/AOV: use ecom_order_total with COUNT(DISTINCT ordernumber)
- For line-level revenue: use netamount (one row per line item)
- ALWAYS filter non-active: WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
- ALWAYS add date range: WHERE orderdate >= DATE_SUB(CURRENT_DATE(), INTERVAL N DAY)
- For customer cohorts/retention: use email as customer identifier
- For median: use APPROX_QUANTILES(col, 100)[OFFSET(50)] (NOT PERCENTILE_CONT with GROUP BY)
- Use SAFE_DIVIDE() for all divisions
- Use DATE_TRUNC(orderdate, MONTH) for monthly grouping

OTHER TABLES (same dataset):
  `frameworkreporting.spree-line-items` — ecommerce line items
  `frameworkreporting.spree-shipments` — shipment tracking
  `frameworkreporting.inventory_snapshot` — inventory levels
  `frameworkreporting.profit-and-loss_load` — P&L data
  `frameworkreporting.netsuite_items` — item master
  `frameworkreporting.campaigns` — marketing campaigns
  `frameworkreporting.campaign_recipients` — campaign recipients
"""


def _extract_sql(text: str) -> str | None:
    """Extract SQL from LLM response, handling markdown fences and preamble."""
    if not text or not text.strip():
        return None

    # Try extracting from markdown code block first
    import re

    match = re.search(r"```(?:sql)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip() or None

    # If response starts with SELECT or WITH, it's raw SQL
    stripped = text.strip()
    if re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
        return stripped

    # Try finding SELECT/WITH statement after preamble text
    match = re.search(r"((?:WITH|SELECT)\b.*)", stripped, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() or None

    return None


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
        f"that answers the user's question.\n\n"
        f"CRITICAL: Output ONLY the raw SQL query. No explanation, no markdown fences, "
        f"no preamble, no comments. Just the SELECT statement.\n\n{dialect_rules}"
    )
    if schema_hint:
        system += f"\n\nAvailable schema:\n{schema_hint}"

    try:
        sql = await _call_haiku(system, question)
        # Extract SQL from response — model may wrap in fences or add preamble
        sql = _extract_sql(sql)
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

    project_id = creds.get("project_id") or (connector.metadata_json or {}).get("project_id", "")
    service_creds = creds.get("service_account_json", {})
    location = creds.get("location") or (connector.metadata_json or {}).get("location")

    # Dry-run cost check
    try:
        cost_info = await estimate_query_cost(
            credentials=service_creds,
            project_id=project_id,
            query=sql,
            location=location,
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
            location=location,
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
    run_id: uuid.UUID | None = None,
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
        "score_sql_match": 0.0,
        "experiment_score": 0.0,
        "baseline_score": 0.0,
        "delta": 0.0,
        "decision": "SKIP",
        "error_message": None,
        "cost_usd": estimate_experiment_cost(case.dialect),
    }

    # Step 1: Generate SQL
    effective_schema_hint = schema_hint
    if not effective_schema_hint:
        if case.dialect == "bigquery":
            effective_schema_hint = _BIGQUERY_SCHEMA_HINT
        elif case.dialect == "suiteql":
            effective_schema_hint = _SUITEQL_SCHEMA_HINT
    generated_sql = await _generate_sql(
        question=case.question,
        dialect=case.dialect,
        schema_hint=effective_schema_hint,
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

    # Step 3: Run vs-MCP benchmark comparison
    # Run our agent (which will use the candidate pattern if it's in the DB)
    # and the Claude+MCP baseline on the same question, then score both
    # with substring_score (fast, deterministic — no LLM judge needed).
    agent_score = 0.0
    bl_score = 0.0

    try:
        agent_result = await run_agent(
            tenant_id=tenant_id,
            question=case.question,
            db=db,
            model="claude-haiku-4-5-20251001",  # cheap model for experiment loop
        )
        if agent_result.success and agent_result.answer_text:
            agent_sr = substring_score(
                answer_text=agent_result.answer_text,
                expected_contains=case.expected_keywords,
            )
            agent_score = agent_sr.score
    except Exception:
        logger.warning(
            "Benchmark agent run failed for question=%s",
            case.question[:60],
            exc_info=True,
        )

    try:
        baseline_result = await run_baseline(
            tenant_id=tenant_id,
            question=case.question,
            db=db,
            model="claude-haiku-4-5-20251001",  # same cheap model for fair comparison
        )
        if baseline_result.success and baseline_result.answer_text:
            baseline_sr = substring_score(
                answer_text=baseline_result.answer_text,
                expected_contains=case.expected_keywords,
            )
            bl_score = baseline_sr.score
    except Exception:
        logger.warning(
            "Benchmark baseline run failed for question=%s",
            case.question[:60],
            exc_info=True,
        )

    result["experiment_score"] = agent_score
    result["baseline_score"] = bl_score
    result["delta"] = round(agent_score - bl_score, 4)

    # Step 4: Decide based on vs-MCP comparison
    if agent_score >= bl_score and agent_score > 0.5:
        result["decision"] = "KEEP"
    elif agent_score < bl_score - 0.1:
        result["decision"] = "REVERT"
    else:
        result["decision"] = "SKIP"

    # Step 5: Promote KEEP results to proven patterns + log all experiments
    # Use test_query key expected by promote_experiment_result
    promote_result = dict(result)
    promote_result["test_query"] = case.question
    await promote_experiment_result(promote_result, tenant_id, db, run_id=run_id)

    return result


# ---------------------------------------------------------------------------
# Promote experiment results to proven patterns
# ---------------------------------------------------------------------------


async def promote_experiment_result(
    result: dict,
    tenant_id: uuid.UUID,
    db: AsyncSession,
    run_id: uuid.UUID | None = None,
) -> None:
    """Promote KEEP experiments to proven patterns and log all experiments.

    Args:
        run_id: When provided (agent-lab UI path), stamped into
            ExperimentLog.metadata_json so get_run_snapshot can find these
            rows via ``metadata_json['run_id'].astext == str(run_id)``.
            Nightly Beat path passes None → metadata_json is unset.
    """

    # Build metadata_json with run_id when called from the agent-lab path.
    # This is what get_run_snapshot queries for kind="experiment".
    metadata = {"run_id": str(run_id)} if run_id is not None else None

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
        metadata_json=metadata,
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
