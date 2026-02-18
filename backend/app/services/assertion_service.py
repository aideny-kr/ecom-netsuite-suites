"""SuiteQL Assertion Service — SELECT-only, table-allowlisted, LIMIT-capped, timeboxed assertions."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service

logger = structlog.get_logger()

# --- Assertion schema ---
ALLOWED_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "between"}
ALLOWED_EXPECT_TYPES = {"row_count", "scalar", "no_rows"}
MAX_ASSERTIONS_PER_RUN = 50
MAX_QUERY_LIMIT = 100
DEFAULT_QUERY_TIMEOUT = 30  # seconds per query


def validate_assertion(assertion: dict[str, Any]) -> None:
    """Validate a single assertion definition."""
    if not assertion.get("name"):
        raise ValueError("Assertion must have a 'name'")
    if not assertion.get("query"):
        raise ValueError(f"Assertion '{assertion['name']}' must have a 'query'")

    expected = assertion.get("expected")
    if not expected or not isinstance(expected, dict):
        raise ValueError(f"Assertion '{assertion['name']}' must have an 'expected' object")

    expect_type = expected.get("type")
    if expect_type not in ALLOWED_EXPECT_TYPES:
        raise ValueError(
            f"Assertion '{assertion['name']}': expected.type must be one of {ALLOWED_EXPECT_TYPES}, got '{expect_type}'"
        )

    operator = expected.get("operator")
    if operator and operator not in ALLOWED_OPERATORS:
        raise ValueError(
            f"Assertion '{assertion['name']}': expected.operator must be one of {ALLOWED_OPERATORS}, got '{operator}'"
        )

    if operator == "between" and expected.get("value2") is None:
        raise ValueError(f"Assertion '{assertion['name']}': 'between' operator requires 'value2'")


def validate_assertions(assertions: list[dict[str, Any]]) -> None:
    """Validate all assertions in a batch."""
    if not assertions:
        raise ValueError("At least one assertion is required")
    if len(assertions) > MAX_ASSERTIONS_PER_RUN:
        raise ValueError(f"Maximum {MAX_ASSERTIONS_PER_RUN} assertions per run")
    for a in assertions:
        validate_assertion(a)


def _evaluate_assertion(expected: dict[str, Any], actual_value: Any) -> bool:
    """Evaluate whether the actual value satisfies the expected condition."""
    expect_type = expected["type"]
    operator = expected.get("operator", "eq")
    target = expected.get("value")

    if expect_type == "no_rows":
        return actual_value == 0

    if expect_type == "row_count":
        count = actual_value
    elif expect_type == "scalar":
        count = actual_value
    else:
        return False

    if operator == "eq":
        return count == target
    elif operator == "ne":
        return count != target
    elif operator == "gt":
        return count > target
    elif operator == "gte":
        return count >= target
    elif operator == "lt":
        return count < target
    elif operator == "lte":
        return count <= target
    elif operator == "between":
        return target <= count <= expected.get("value2", target)
    return False


async def execute_assertions(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    assertions: list[dict[str, Any]],
    suiteql_executor,
    correlation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Execute a batch of SuiteQL assertions and return the report.

    suiteql_executor: async callable(query, limit, timeout) -> dict with keys:
        'rows', 'row_count', 'error', 'columns'
    """
    from app.core.config import settings
    from app.mcp.tools.netsuite_suiteql import enforce_limit, is_read_only_sql, parse_tables

    allowed_tables = {t.strip().lower() for t in settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")}

    results: list[dict[str, Any]] = []
    total_passed = 0
    total_failed = 0
    total_errors = 0
    start_time = time.monotonic()

    for assertion in assertions:
        a_start = time.monotonic()
        name = assertion["name"]
        query = assertion["query"]
        expected = assertion["expected"]

        result_entry: dict[str, Any] = {
            "name": name,
            "query": query,
            "expected": expected,
        }

        # Validate SELECT-only
        if not is_read_only_sql(query):
            result_entry.update(
                status="error",
                error="Only SELECT queries are permitted",
                duration_ms=int((time.monotonic() - a_start) * 1000),
            )
            total_errors += 1
            results.append(result_entry)
            await _audit_assertion(
                db, tenant_id, run_id, name, "error", "Non-SELECT query blocked", correlation_id, actor_id
            )
            continue

        # Validate table allowlist
        tables = parse_tables(query)
        disallowed = tables - allowed_tables
        if disallowed:
            result_entry.update(
                status="error",
                error=f"Disallowed tables: {', '.join(sorted(disallowed))}",
                duration_ms=int((time.monotonic() - a_start) * 1000),
            )
            total_errors += 1
            results.append(result_entry)
            await _audit_assertion(
                db,
                tenant_id,
                run_id,
                name,
                "error",
                f"Disallowed tables: {', '.join(sorted(disallowed))}",
                correlation_id,
                actor_id,
            )
            continue

        # Enforce LIMIT
        capped_query = enforce_limit(query, MAX_QUERY_LIMIT)

        # Execute query
        try:
            qr = await suiteql_executor(capped_query, MAX_QUERY_LIMIT, DEFAULT_QUERY_TIMEOUT)
        except Exception as exc:
            result_entry.update(
                status="error",
                error=str(exc),
                duration_ms=int((time.monotonic() - a_start) * 1000),
            )
            total_errors += 1
            results.append(result_entry)
            await _audit_assertion(db, tenant_id, run_id, name, "error", str(exc), correlation_id, actor_id)
            continue

        if qr.get("error"):
            result_entry.update(
                status="error",
                error=qr.get("message", "Query failed"),
                duration_ms=int((time.monotonic() - a_start) * 1000),
            )
            total_errors += 1
            results.append(result_entry)
            await _audit_assertion(
                db, tenant_id, run_id, name, "error", qr.get("message", "Query failed"), correlation_id, actor_id
            )
            continue

        # Compute actual value (redacted — counts/scalars only)
        row_count = qr.get("row_count", 0)
        if expected["type"] == "scalar":
            rows = qr.get("rows", [])
            actual_value = rows[0][0] if rows and rows[0] else None
        else:
            actual_value = row_count

        passed = _evaluate_assertion(expected, actual_value)
        status = "passed" if passed else "failed"

        if passed:
            total_passed += 1
        else:
            total_failed += 1

        duration_ms = int((time.monotonic() - a_start) * 1000)
        result_entry.update(
            status=status,
            actual_value=actual_value,
            row_count=row_count,
            duration_ms=duration_ms,
        )
        results.append(result_entry)
        await _audit_assertion(db, tenant_id, run_id, name, status, None, correlation_id, actor_id)

    total_duration_ms = int((time.monotonic() - start_time) * 1000)

    report = {
        "run_id": str(run_id),
        "tenant_id": str(tenant_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "assertions": results,
        "summary": {
            "total": len(assertions),
            "passed": total_passed,
            "failed": total_failed,
            "errors": total_errors,
        },
        "overall_status": "passed" if total_failed == 0 and total_errors == 0 else "failed",
        "total_duration_ms": total_duration_ms,
    }
    return report


async def _audit_assertion(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    assertion_name: str,
    status: str,
    error_message: str | None,
    correlation_id: str | None,
    actor_id: uuid.UUID | None,
) -> None:
    """Emit audit event for a single assertion execution."""
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="workspace",
        action="suiteql.assertion.executed",
        actor_id=actor_id,
        resource_type="workspace_run",
        resource_id=str(run_id),
        correlation_id=correlation_id,
        payload={"assertion_name": assertion_name, "status": status},
        status=status,
        error_message=error_message,
    )
