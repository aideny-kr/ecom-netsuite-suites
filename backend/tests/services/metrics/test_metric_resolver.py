# backend/tests/services/metrics/test_metric_resolver.py
import math

import pytest
from sqlalchemy import delete, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_resolver import resolve_metrics


# ---------------------------------------------------------------------------
# Deterministic vector helpers for M2 threshold tests
#
# pgvector cosine_distance = 1 - cosine_similarity.
# We build unit vectors in orthogonal directions so cosine_distance between
# different "concept families" is exactly 1.0 (fully orthogonal) and within
# a concept family is 0.0 (identical) or near-zero (similar tilt).
#
# DIM = 1536 to match MetricDefinition.intent_embedding (Vector(1536)).
# ---------------------------------------------------------------------------
_DIM = 1536


def _unit_vec(hot_index: int) -> list[float]:
    """Return a 1536-d unit vector with 1.0 at ``hot_index`` and 0 elsewhere."""
    v = [0.0] * _DIM
    v[hot_index] = 1.0
    return v


def _near_vec(hot_index: int, tilt_index: int, tilt: float = 0.1) -> list[float]:
    """A unit vector mostly in direction ``hot_index`` with a small tilt toward
    ``tilt_index``.  cosine_distance to _unit_vec(hot_index) ≈ 1-cos(arctan(tilt)).
    Both indices must differ.
    """
    v = [0.0] * _DIM
    v[hot_index] = 1.0
    v[tilt_index] = tilt
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v]


# Finance metrics live in direction 0; unrelated queries live in direction 1.
_VEC_FINANCE = _unit_vec(0)  # cosine_distance to _VEC_FINANCE_NEAR ≈ very small
_VEC_FINANCE_NEAR = _near_vec(0, 2, tilt=0.2)  # cosine_sim ≈ 0.98 → distance ≈ 0.02
_VEC_UNRELATED = _unit_vec(1)  # cosine_distance to _VEC_FINANCE = 1.0 (orthogonal)


async def _clear_catalog(db):
    # Test hygiene: these tests insert SYSTEM rows whose keys collide on
    # UNIQUE(tenant_id, key) with the system seeder's keys if the catalog is
    # already seeded. Clear the catalog first (rolled back per the db fixture).
    await db.execute(delete(MetricDefinition))
    await db.flush()


async def _ensure_system_tenant(db):
    # SYSTEM-default metric rows FK to tenants.id; seed the canonical SYSTEM tenant
    # parent row (rolled back per test by the db fixture) so the insert is valid.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()


async def _add(db, tenant_id, key, synonyms=None):
    if tenant_id == SYSTEM_TENANT_ID:
        await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=tenant_id,
            key=key,
            display_name=key.replace("_", " ").title(),
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="a/b",
            depends_on=["a", "b"],
            synonyms=synonyms or [],
            status="active",
            version=1,
        )
    )
    await db.flush()


async def test_resolve_returns_system_and_tenant_excludes_other(db, tenant_a, tenant_b):
    await _clear_catalog(db)
    await _add(db, SYSTEM_TENANT_ID, "gross_revenue")
    await _add(db, tenant_a.id, "net_margin", synonyms=["bottom line margin"])
    await _add(db, tenant_b.id, "secret_metric")

    keys = {m.key for m in await resolve_metrics(db, tenant_id=tenant_a.id, query="bottom line margin", top_k=10)}
    assert "net_margin" in keys  # tenant synonym match
    # gross_revenue is a SYSTEM metric but is NOT matched by "bottom line margin" —
    # resolution is now relevance-bounded (R1#2): unmatched SYSTEM rows are not dumped.
    assert "gross_revenue" not in keys
    assert "secret_metric" not in keys  # other tenant excluded


async def test_tenant_override_wins_by_key(db, tenant_a):
    await _clear_catalog(db)
    await _add(db, SYSTEM_TENANT_ID, "net_margin")
    await _add(db, tenant_a.id, "net_margin")
    matches = await resolve_metrics(db, tenant_id=tenant_a.id, query="net_margin", top_k=10)
    nm = [m for m in matches if m.key == "net_margin"]
    assert len(nm) == 1 and nm[0].tenant_id == tenant_a.id


async def test_unknown_query_does_not_dump_all_system_metrics(db):
    await _clear_catalog(db)
    await _ensure_system_tenant(db)
    import uuid as _uuid

    for k in ("cash", "ar", "ap"):
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=k,
                display_name=k,
                definition=k,
                unit="currency",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
                params_schema={"period": {"type": "period"}},
                status="active",
                version=1,
                provenance={"author": "t"},
            )
        )
    await db.flush()
    rows = await resolve_metrics(db, tenant_id=_uuid.uuid4(), query="airspeed velocity of a swallow")
    assert rows == [], "an unrelated ask must NOT return the full blessed SYSTEM set (R1#2)"


async def test_exact_key_survives_topk(db):
    await _clear_catalog(db)
    await _ensure_system_tenant(db)
    import uuid as _uuid

    for i in range(8):
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=f"m{i}",
                display_name=f"m{i}",
                definition="x",
                unit="count",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
                params_schema={"period": {"type": "period"}},
                status="active",
                version=1,
                provenance={"author": "t"},
            )
        )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_margin",
            display_name="Net Margin",
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="a/b",
            depends_on=["a", "b"],
            status="active",
            version=1,
            provenance={"author": "t"},
        )
    )
    await db.flush()
    rows = await resolve_metrics(db, tenant_id=_uuid.uuid4(), query="net_margin", top_k=5)
    assert any(r.key == "net_margin" for r in rows), (
        "exact key match must never be truncated behind vector noise (R1#3)"
    )


# ---------------------------------------------------------------------------
# M2 — cosine-distance similarity threshold on the vector branch
# ---------------------------------------------------------------------------


async def _add_with_embedding(db, tenant_id, key, embedding: list[float], synonyms=None):
    """Seed a MetricDefinition with a pre-computed embedding vector."""
    if tenant_id == SYSTEM_TENANT_ID:
        await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=tenant_id,
            key=key,
            display_name=key.replace("_", " ").title(),
            definition="x",
            unit="currency",
            source_kind="expression",
            expression="a/b",
            depends_on=["a", "b"],
            synonyms=synonyms or [],
            intent_embedding=embedding,
            status="active",
            version=1,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_unrelated_query_below_threshold_returns_nothing(db, monkeypatch):
    """M2 (non-vacuous): finance metrics have VEC_FINANCE embeddings; an unrelated
    query maps to VEC_UNRELATED (cosine_distance = 1.0 >> threshold).  The vector
    branch must return nothing; no key/synonym match exists either → result is [].

    Previously (no threshold) the resolver returned the nearest finance rows even
    for an orthogonal query.  After adding _SIMILARITY_MAX_DISTANCE the WHERE filter
    excludes them.
    """
    import uuid as _uuid

    await _clear_catalog(db)
    await _ensure_system_tenant(db)

    # Seed finance metrics with VEC_FINANCE embeddings.
    for key in ("gross_revenue", "net_revenue", "cash_flow"):
        await _add_with_embedding(db, SYSTEM_TENANT_ID, key, _VEC_FINANCE)

    # Patch embed_domain_query: "airspeed velocity" → VEC_UNRELATED (orthogonal to finance).
    async def _fake_embed(text: str) -> list[float]:
        return _VEC_UNRELATED

    monkeypatch.setattr("app.services.metrics.metric_resolver.embed_domain_query", _fake_embed)

    rows = await resolve_metrics(db, tenant_id=_uuid.uuid4(), query="airspeed velocity of a swallow")
    assert rows == [], "M2: vector branch must respect cosine-distance threshold — orthogonal query must return nothing"


@pytest.mark.asyncio
async def test_related_query_above_threshold_returns_match(db, monkeypatch):
    """M2: a query whose embedding is NEAR a seeded metric (cosine_distance << threshold)
    must still be returned by the vector branch.
    """
    import uuid as _uuid

    await _clear_catalog(db)
    await _ensure_system_tenant(db)

    # Seed one finance metric with the exact VEC_FINANCE embedding.
    await _add_with_embedding(db, SYSTEM_TENANT_ID, "gross_revenue", _VEC_FINANCE)

    # Query embedding is VEC_FINANCE_NEAR (cosine_distance ≈ 0.02 << 0.55 threshold).
    async def _fake_embed(text: str) -> list[float]:
        return _VEC_FINANCE_NEAR

    monkeypatch.setattr("app.services.metrics.metric_resolver.embed_domain_query", _fake_embed)

    rows = await resolve_metrics(db, tenant_id=_uuid.uuid4(), query="total revenue this quarter")
    assert any(r.key == "gross_revenue" for r in rows), (
        "M2: a near-match (cosine_distance < threshold) must be returned by the vector branch"
    )


@pytest.mark.asyncio
async def test_exact_key_still_resolves_regardless_of_threshold(db, monkeypatch):
    """M2: the keyword/exact branch is UNCHANGED — it bypasses the vector threshold.
    Even when the query embedding would be far (VEC_UNRELATED), an exact key match
    must still be returned via the keyword path.
    """
    import uuid as _uuid

    await _clear_catalog(db)
    await _ensure_system_tenant(db)

    # Seed a metric with VEC_FINANCE embedding (far from VEC_UNRELATED).
    await _add_with_embedding(db, SYSTEM_TENANT_ID, "gross_revenue", _VEC_FINANCE)

    # The embed returns VEC_UNRELATED, but the query text is the exact key.
    async def _fake_embed(text: str) -> list[float]:
        return _VEC_UNRELATED

    monkeypatch.setattr("app.services.metrics.metric_resolver.embed_domain_query", _fake_embed)

    # "gross_revenue" is an exact key match → keyword branch fires regardless of vector distance.
    rows = await resolve_metrics(db, tenant_id=_uuid.uuid4(), query="gross_revenue")
    assert any(r.key == "gross_revenue" for r in rows), (
        "M2: exact key match must resolve via keyword branch independent of vector threshold"
    )
