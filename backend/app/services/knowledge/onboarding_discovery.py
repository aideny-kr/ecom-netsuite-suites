"""Tenant Onboarding Deep Discovery — eliminates cold start for new tenants."""

import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


@dataclass
class PhaseResult:
    name: str
    success: bool = True
    data: dict | list | None = None
    duration_ms: int = 0
    error: str | None = None


@dataclass
class OnboardingResult:
    tenant_id: str
    phases: list[PhaseResult] = field(default_factory=list)
    total_duration_ms: int = 0
    queries_executed: int = 0


async def run_onboarding_discovery(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    access_token: str,
    account_id: str,
) -> OnboardingResult:
    """Run 6-phase deep discovery for a new tenant."""
    result = OnboardingResult(tenant_id=str(tenant_id))
    start = time.monotonic()

    # Phase 1: Transaction Landscape
    phase1 = await _discover_transaction_types(access_token, account_id)
    result.phases.append(phase1)
    result.queries_executed += 1

    # Phase 2: Transaction Relationships
    phase2 = await _discover_relationships(access_token, account_id)
    result.phases.append(phase2)
    result.queries_executed += 1

    # Phase 3: Custom Field Usage (deferred to metadata discovery)
    phase3 = PhaseResult(
        name="custom_field_usage",
        success=True,
        data=[],
        error="Deferred to metadata discovery",
    )
    result.phases.append(phase3)

    # Phase 4: Status Code Mapping
    transaction_types = phase1.data if isinstance(phase1.data, list) else []
    top_types = [t["type"] for t in transaction_types[:10] if t.get("count", 0) > 10]
    phase4 = await _discover_status_codes(access_token, account_id, top_types)
    result.phases.append(phase4)
    result.queries_executed += len(top_types)

    # Phase 5: Sample Queries — deferred (requires Haiku LLM call)
    phase5 = PhaseResult(
        name="sample_queries",
        success=True,
        data=[],
        error="Deferred to Phase 2",
    )
    result.phases.append(phase5)

    # Phase 6: Saved Searches Inventory — deferred (requires MCP tools)
    phase6 = PhaseResult(
        name="saved_searches",
        success=True,
        data=[],
        error="Requires MCP — deferred",
    )
    result.phases.append(phase6)

    result.total_duration_ms = int((time.monotonic() - start) * 1000)

    # Store profile in tenant_configs
    profile = {
        "transaction_types": phase1.data if phase1.success else [],
        "transaction_relationships": phase2.data if phase2.success else [],
        "status_codes": phase4.data if phase4.success else {},
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_ms": result.total_duration_ms,
        "queries_executed": result.queries_executed,
    }

    from app.models.tenant import TenantConfig
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    tc_result = await db.execute(
        select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
    )
    tenant_config = tc_result.scalar_one_or_none()
    if tenant_config:
        tenant_config.onboarding_profile = profile
        flag_modified(tenant_config, "onboarding_profile")
        await db.commit()

    logger.info(
        "onboarding_discovery.complete",
        tenant_id=str(tenant_id),
        phases=len(result.phases),
        queries=result.queries_executed,
        duration_ms=result.total_duration_ms,
    )

    return result


async def _discover_transaction_types(access_token: str, account_id: str) -> PhaseResult:
    """Phase 1: What transaction types does this tenant use?"""
    from app.services.netsuite_client import execute_suiteql_via_rest

    t0 = time.monotonic()
    try:
        query = """
        SELECT type, COUNT(*) as cnt,
               TO_CHAR(MIN(trandate), 'YYYY-MM-DD') as earliest,
               TO_CHAR(MAX(trandate), 'YYYY-MM-DD') as latest
        FROM transaction
        GROUP BY type
        ORDER BY cnt DESC
        """
        result = await execute_suiteql_via_rest(access_token, account_id, query, limit=50)
        rows = result.get("rows", [])

        types = []
        for row in rows:
            if len(row) >= 4:
                types.append({
                    "type": row[0],
                    "count": row[1],
                    "earliest": row[2],
                    "latest": row[3],
                })

        return PhaseResult(
            name="transaction_landscape",
            success=True,
            data=types,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as e:
        return PhaseResult(
            name="transaction_landscape",
            success=False,
            error=str(e)[:200],
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


async def _discover_relationships(access_token: str, account_id: str) -> PhaseResult:
    """Phase 2: How are transaction types linked via createdfrom?"""
    from app.services.netsuite_client import execute_suiteql_via_rest

    t0 = time.monotonic()
    try:
        query = """
        SELECT parent.type as source_type,
               child.type as created_type,
               COUNT(*) as link_count
        FROM transaction child
        JOIN transaction parent ON parent.id = child.createdfrom
        WHERE child.createdfrom IS NOT NULL
        GROUP BY parent.type, child.type
        ORDER BY link_count DESC
        FETCH FIRST 50 ROWS ONLY
        """
        result = await execute_suiteql_via_rest(access_token, account_id, query, limit=50)
        rows = result.get("rows", [])

        relationships = []
        for row in rows:
            if len(row) >= 3:
                relationships.append({
                    "source": row[0],
                    "target": row[1],
                    "count": row[2],
                })

        return PhaseResult(
            name="transaction_relationships",
            success=True,
            data=relationships,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as e:
        return PhaseResult(
            name="transaction_relationships",
            success=False,
            error=str(e)[:200],
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


async def _discover_status_codes(
    access_token: str, account_id: str, transaction_types: list[str]
) -> PhaseResult:
    """Phase 4: What do status codes mean for each transaction type?"""
    from app.services.netsuite_client import execute_suiteql_via_rest

    t0 = time.monotonic()
    status_map: dict[str, list[dict]] = {}

    for txn_type in transaction_types:
        try:
            query = f"""
            SELECT status, BUILTIN.DF(status) as status_display, COUNT(*) as cnt
            FROM transaction
            WHERE type = '{txn_type}'
            GROUP BY status, BUILTIN.DF(status)
            ORDER BY cnt DESC
            FETCH FIRST 15 ROWS ONLY
            """
            result = await execute_suiteql_via_rest(access_token, account_id, query, limit=15)
            rows = result.get("rows", [])

            codes = []
            for row in rows:
                if len(row) >= 3:
                    codes.append({
                        "code": row[0],
                        "display": row[1],
                        "count": row[2],
                    })

            if codes:
                status_map[txn_type] = codes
        except Exception:
            continue

    return PhaseResult(
        name="status_code_mapping",
        success=True,
        data=status_map,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
