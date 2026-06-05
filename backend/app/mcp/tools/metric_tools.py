"""Chat-tool adapters for the metric catalog. Thin wrappers over the metric services."""

import uuid
from typing import Any

from app.services.metrics.metric_resolver import resolve_metrics


async def resolve(params: dict, context: dict | None = None, **kwargs: Any) -> dict:
    ctx = context or {}
    matches = await resolve_metrics(
        ctx["db"],
        tenant_id=uuid.UUID(str(ctx["tenant_id"])),
        query=params["query"],
        top_k=int(params.get("top_k", 5)),
    )
    return {
        "metrics": [
            {
                "key": m.key,
                "display_name": m.display_name,
                "definition": m.definition,
                "unit": m.unit,
                "source_kind": m.source_kind,
                "params_schema": m.params_schema or {},
                "dimensions": m.dimensions or {},
                "status": m.status,
            }
            for m in matches
        ],
        "note": "Definitions are DISPLAY-ONLY. Do not compute a number from them; call metric.compute.",
    }


async def compute(params: dict, context: dict | None = None, **kwargs: Any) -> dict:
    # Lazy import: compute_metric is the DB-backed orchestration that lives in
    # metric_compute (added in Task 6 notes / exercised in Task 11). Importing it
    # at call time keeps registry module load working before that lands.
    from app.services.metrics.metric_compute import compute_metric

    ctx = context or {}
    tenant_id = uuid.UUID(str(ctx["tenant_id"]))

    # F2: thread the tenant's fiscal_year_start_month into the compute context.
    # The production tool seam (governance.governed_execute) builds the context
    # dict WITHOUT a fiscal month, so without this every period resolves to the
    # calendar year — wrong windows for any non-January-fiscal tenant. The chat
    # orchestrator pre-injects this value, so we only fetch when it is absent (no
    # extra query on the hot path) and never overwrite an explicit value.
    if "fiscal_year_start_month" not in ctx:
        ctx = {**ctx, "fiscal_year_start_month": await _tenant_fiscal_month(ctx.get("db"), tenant_id)}

    return await compute_metric(
        ctx["db"],
        tenant_id=tenant_id,
        key=params["key"],
        params=params.get("params", {}),
        context=ctx,
    )


async def _tenant_fiscal_month(db, tenant_id: uuid.UUID) -> int:
    """Read tenant_configs.fiscal_year_start_month (the fiscal-calendar source of
    truth) for `tenant_id`, defaulting to 1 (calendar year) when there is no DB
    handle or no config row. Keeps the period resolver fiscal-correct on the
    governed_execute seam, which does not carry the value in its context."""
    if db is None:
        return 1
    from sqlalchemy import select

    from app.models.tenant import TenantConfig

    fy = (
        await db.execute(select(TenantConfig.fiscal_year_start_month).where(TenantConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    return int(fy or 1)
