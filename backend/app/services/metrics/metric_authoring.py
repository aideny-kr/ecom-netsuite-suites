# backend/app/services/metrics/metric_authoring.py
"""Author-time validation for metric definitions (one-of, key-allowlist, DAG, params)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.chat.domain_knowledge import embed_domain_query
from app.services.metrics.expression_evaluator import ExpressionError, extract_dependencies
from app.services.metrics.system_tenant import ensure_system_tenant

_SINGLE_SOURCE_KEYS = {"query", "dialect"}


class AuthoringError(ValueError):
    pass


def validate_definition(d: dict, *, allowed_cross_source_keys: set[str] | None = None) -> None:
    kind = d.get("source_kind")
    spec, expr = d.get("blessed_spec"), d.get("expression")

    if bool(spec) == bool(expr):
        raise AuthoringError("exactly one of blessed_spec / expression must be set")

    if kind == "expression":
        if not expr or not d.get("depends_on"):
            raise AuthoringError("expression metrics need expression + depends_on")
        try:
            deps = set(extract_dependencies(expr))
        except ExpressionError as ex:
            raise AuthoringError(str(ex)) from ex
        if d["key"] in deps:
            raise AuthoringError("expression cannot reference itself (cycle)")
        if deps != set(d["depends_on"]):
            raise AuthoringError("depends_on must match expression references")
    else:
        if not isinstance(spec, dict):
            raise AuthoringError("query-backed metric needs a blessed_spec object")
        allowed = _SINGLE_SOURCE_KEYS if kind in ("suiteql", "bigquery") else (allowed_cross_source_keys or set())
        unknown = set(spec) - allowed
        if unknown:
            raise AuthoringError(f"blessed_spec has keys not in the live tool schema: {sorted(unknown)}")


def _embed_text(payload: dict) -> str:
    parts = [payload.get("display_name", ""), payload.get("definition", "")]
    parts.extend(payload.get("synonyms") or [])
    return " | ".join(p for p in parts if p)


async def create_metric(db: AsyncSession, *, tenant_id: uuid.UUID, payload: dict) -> MetricDefinition:
    """Persist a tenant (or SYSTEM) metric definition with a 1536-d intent embedding."""
    # Defense-in-depth so the authoring CLI is self-sufficient: a SYSTEM-default row
    # FKs to the synthetic SYSTEM tenant, which may not exist yet on a fresh DB.
    if tenant_id == SYSTEM_TENANT_ID:
        await ensure_system_tenant(db)
        await db.flush()
    embedding = await embed_domain_query(_embed_text(payload))
    metric = MetricDefinition(
        tenant_id=tenant_id,
        key=payload["key"],
        display_name=payload["display_name"],
        definition=payload["definition"],
        unit=payload["unit"],
        source_kind=payload["source_kind"],
        blessed_spec=payload.get("blessed_spec"),
        expression=payload.get("expression"),
        depends_on=payload.get("depends_on"),
        params_schema=payload.get("params_schema"),
        dimensions=payload.get("dimensions"),
        synonyms=payload.get("synonyms"),
        intent_embedding=embedding,
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()
    return metric
