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
    return await compute_metric(
        ctx["db"],
        tenant_id=uuid.UUID(str(ctx["tenant_id"])),
        key=params["key"],
        params=params.get("params", {}),
        context=ctx,
    )
