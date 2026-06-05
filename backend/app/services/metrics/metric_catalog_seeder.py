"""Seed system-default finance metrics (SYSTEM_TENANT_ID) with 1536-d embeddings. Idempotent."""

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.chat.domain_knowledge import embed_domain_texts
from app.services.metrics.system_tenant import ensure_system_tenant

# Small generic core + a couple DTC-flavored extras (spec §13 Q1). Expression leaves reference other keys.
_SYSTEM_METRICS: list[dict] = [
    {
        "key": "gross_revenue",
        "display_name": "Gross Revenue",
        "unit": "currency",
        "definition": "Total revenue before returns/discounts.",
        "source_kind": "suiteql",
        "synonyms": ["revenue", "sales", "top line"],
    },
    {
        "key": "net_revenue",
        "display_name": "Net Revenue",
        "unit": "currency",
        "definition": "Revenue net of returns and discounts.",
        "source_kind": "suiteql",
        "synonyms": ["net sales"],
    },
    {
        "key": "gross_income",
        "display_name": "Gross Income",
        "unit": "currency",
        "definition": "Net revenue minus COGS.",
        "source_kind": "suiteql",
        "synonyms": ["gross profit"],
    },
    {
        "key": "net_income",
        "display_name": "Net Income",
        "unit": "currency",
        "definition": "Bottom-line profit after all expenses.",
        "source_kind": "suiteql",
        "synonyms": ["net profit", "bottom line"],
    },
    {
        "key": "gross_margin",
        "display_name": "Gross Margin",
        "unit": "percent",
        "definition": "Gross income divided by net revenue.",
        "source_kind": "expression",
        "expression": "gross_income / net_revenue",
        "depends_on": ["gross_income", "net_revenue"],
        "synonyms": ["gross margin pct"],
    },
    {
        "key": "net_margin",
        "display_name": "Net Margin",
        "unit": "percent",
        "definition": "Net income divided by gross revenue.",
        "source_kind": "expression",
        "expression": "net_income / gross_revenue",
        "depends_on": ["net_income", "gross_revenue"],
        "synonyms": ["net profit margin", "bottom line margin"],
    },
    {
        "key": "ar",
        "display_name": "Accounts Receivable",
        "unit": "currency",
        "definition": "Outstanding customer receivables.",
        "source_kind": "suiteql",
        "synonyms": ["receivables"],
    },
    {
        "key": "ap",
        "display_name": "Accounts Payable",
        "unit": "currency",
        "definition": "Outstanding vendor payables.",
        "source_kind": "suiteql",
        "synonyms": ["payables"],
    },
    {
        "key": "cash",
        "display_name": "Cash",
        "unit": "currency",
        "definition": "Cash and cash equivalents balance.",
        "source_kind": "suiteql",
        "synonyms": ["cash balance"],
    },
]


def _embed_text(m: dict) -> str:
    return " | ".join([m["display_name"], m["definition"], *m.get("synonyms", [])])


async def seed_system_metrics(db: AsyncSession) -> int:
    # FORCE RLS (mig 081) applies to metric_definitions for non-superuser roles
    # (Supabase); set SYSTEM context so the policy's OR-SYSTEM clause permits the
    # seed writes and get_current_tenant_id() doesn't throw on an unset GUC.
    await set_tenant_context(db, str(SYSTEM_TENANT_ID))

    # Defense-in-depth: SYSTEM metric rows FK to tenants.id; provision the parent
    # so the seeder is self-sufficient even on a fresh DB (mig 080 also seeds it).
    await ensure_system_tenant(db)
    await db.flush()

    embeddings = await embed_domain_texts([_embed_text(m) for m in _SYSTEM_METRICS])
    if embeddings is None:
        raise RuntimeError("seeder requires the 1536-d embedder; refusing to seed rows without embeddings (§12.2)")

    for idx, m in enumerate(_SYSTEM_METRICS):
        vec = embeddings[idx]
        assert len(vec) == 1536, "embedding must be 1536-d (use embed_domain_*)"
        # D3: query-backed placeholders (SELECT 0 stubs) seed as "draft" so they are
        # discoverable for authoring but never returned as a computed (zero) answer.
        # Expression metrics whose leaves are draft yield missing_dependency — also safe.
        is_placeholder = m["source_kind"] in ("suiteql", "bigquery")
        values = {
            "tenant_id": SYSTEM_TENANT_ID,
            "key": m["key"],
            "display_name": m["display_name"],
            "definition": m["definition"],
            "unit": m["unit"],
            "source_kind": m["source_kind"],
            "blessed_spec": ({"query": "SELECT 0", "dialect": "suiteql"} if m["source_kind"] == "suiteql" else None),
            "expression": m.get("expression"),
            "depends_on": m.get("depends_on"),
            "params_schema": {"period": {"type": "period"}},
            "synonyms": m.get("synonyms", []),
            "intent_embedding": vec,
            "status": "draft" if is_placeholder else "active",
            "version": 1,
            "provenance": {"author": "system_seed"},
        }
        # R3#25: use ON CONFLICT DO UPDATE so two concurrent seeders converge
        # rather than racing into a UNIQUE(tenant_id, key) violation.
        # All mutable seed columns are refreshed on conflict so re-seeding after
        # a definition update propagates correctly.
        stmt = (
            pg_insert(MetricDefinition)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["tenant_id", "key"],
                set_={
                    "display_name": values["display_name"],
                    "definition": values["definition"],
                    "unit": values["unit"],
                    "source_kind": values["source_kind"],
                    "blessed_spec": values["blessed_spec"],
                    "expression": values["expression"],
                    "depends_on": values["depends_on"],
                    "params_schema": values["params_schema"],
                    "synonyms": values["synonyms"],
                    "intent_embedding": values["intent_embedding"],
                    "status": values["status"],
                    "version": values["version"],
                    "provenance": values["provenance"],
                },
            )
        )
        await db.execute(stmt)

    return len(_SYSTEM_METRICS)
