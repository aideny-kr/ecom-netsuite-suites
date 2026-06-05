"""Seed system-default finance metrics (SYSTEM_TENANT_ID) with 1536-d embeddings. Idempotent."""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

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
    # Defense-in-depth: SYSTEM metric rows FK to tenants.id; provision the parent
    # so the seeder is self-sufficient even on a fresh DB (mig 080 also seeds it).
    await ensure_system_tenant(db)
    await db.flush()
    await db.execute(delete(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID))
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
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=m["key"],
                display_name=m["display_name"],
                definition=m["definition"],
                unit=m["unit"],
                source_kind=m["source_kind"],
                blessed_spec=({"query": "SELECT 0", "dialect": "suiteql"} if m["source_kind"] == "suiteql" else None),
                expression=m.get("expression"),
                depends_on=m.get("depends_on"),
                params_schema={"period": {"type": "period"}},
                synonyms=m.get("synonyms", []),
                intent_embedding=vec,
                status="draft" if is_placeholder else "active",
                version=1,
                provenance={"author": "system_seed"},
            )
        )
    return len(_SYSTEM_METRICS)
