"""API tests for the tenant memory graph router.

Mirrors tests/api/test_metrics_api.py fixtures (client/admin_user/member_user/
admin_user_b/db). Covers: list graph; cross-tenant concept id -> 404; member
(no memory.manage) PATCH -> 403; DELETE is soft (row remains, review_state
flips to 'rejected').
"""

import uuid

import pytest_asyncio
from sqlalchemy import select

from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_edge import TenantMemoryEdge
from app.models.tenant_memory_link import TenantMemoryLink


async def _seed_concept(db, tenant_id, *, name="Net Revenue", review_state="pending", summary="excludes refunds"):
    c = TenantMemoryConcept(
        tenant_id=tenant_id,
        name=name,
        summary=summary,
        concept_type="definition",
        review_state=review_state,
    )
    db.add(c)
    await db.flush()
    return c


@pytest_asyncio.fixture
async def seeded_concept(db, admin_user):
    user, _ = admin_user
    return await _seed_concept(db, user.tenant_id, name="Seeded Concept")


async def test_list_graph_returns_concepts_and_edges(client, admin_user, db):
    user, headers = admin_user
    a = await _seed_concept(db, user.tenant_id, name="Concept A", review_state="confirmed")
    b = await _seed_concept(db, user.tenant_id, name="Concept B", review_state="confirmed")
    edge = TenantMemoryEdge(
        tenant_id=user.tenant_id,
        source_concept_id=a.id,
        target_concept_id=b.id,
        relation="relates_to",
    )
    db.add(edge)
    await db.flush()

    resp = await client.get("/api/v1/tenant-memory", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {c["name"] for c in body["concepts"]}
    assert {"Concept A", "Concept B"} <= names
    assert any(e["source_concept_id"] == str(a.id) for e in body["edges"])
    # UUIDs are serialized as strings.
    assert all(isinstance(c["id"], str) for c in body["concepts"])


async def test_list_graph_filters_review_state(client, admin_user, db):
    user, headers = admin_user
    await _seed_concept(db, user.tenant_id, name="P concept", review_state="pending")
    await _seed_concept(db, user.tenant_id, name="C concept", review_state="confirmed")

    resp = await client.get("/api/v1/tenant-memory?review_state=confirmed", headers=headers)
    assert resp.status_code == 200, resp.text
    names = {c["name"] for c in resp.json()["concepts"]}
    assert "C concept" in names
    assert "P concept" not in names


async def test_get_concept_detail_includes_links(client, admin_user, db):
    user, headers = admin_user
    c = await _seed_concept(db, user.tenant_id)
    link = TenantMemoryLink(
        tenant_id=user.tenant_id,
        concept_id=c.id,
        source_table="tenant_learned_rules",
        source_id=uuid.uuid4(),
    )
    db.add(link)
    await db.flush()

    resp = await client.get(f"/api/v1/tenant-memory/concepts/{c.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(c.id)
    assert len(body["links"]) == 1
    assert body["links"][0]["source_table"] == "tenant_learned_rules"


async def test_cross_tenant_concept_is_404(client, admin_user, admin_user_b, db):
    _, headers_a = admin_user
    user_b, _ = admin_user_b
    theirs = await _seed_concept(db, user_b.tenant_id, name="Tenant B Concept")

    resp = await client.get(f"/api/v1/tenant-memory/concepts/{theirs.id}", headers=headers_a)
    assert resp.status_code == 404, resp.text


async def test_get_concept_malformed_uuid_is_404(client, admin_user):
    _, headers = admin_user
    resp = await client.get("/api/v1/tenant-memory/concepts/not-a-uuid", headers=headers)
    assert resp.status_code == 404, resp.text


async def test_member_patch_forbidden(client, member_user):
    _, headers = member_user
    resp = await client.patch(
        f"/api/v1/tenant-memory/concepts/{uuid.uuid4()}",
        json={"review_state": "confirmed"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


async def test_patch_confirm_sets_confirmed_by(client, admin_user, db, seeded_concept):
    user, headers = admin_user
    resp = await client.patch(
        f"/api/v1/tenant-memory/concepts/{seeded_concept.id}",
        json={"review_state": "confirmed"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_state"] == "confirmed"

    row = (
        await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.id == seeded_concept.id))
    ).scalar_one()
    await db.refresh(row)
    assert row.confirmed_by == user.id


async def test_patch_cross_tenant_404(client, admin_user, admin_user_b, db):
    _, headers_a = admin_user
    user_b, _ = admin_user_b
    theirs = await _seed_concept(db, user_b.tenant_id, name="B Concept")

    resp = await client.patch(
        f"/api/v1/tenant-memory/concepts/{theirs.id}",
        json={"name": "Hijacked"},
        headers=headers_a,
    )
    assert resp.status_code == 404, resp.text
    # Untouched.
    row = (await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.id == theirs.id))).scalar_one()
    assert row.name == "B Concept"


async def test_patch_rejects_invalid_review_state(client, admin_user, seeded_concept):
    _, headers = admin_user
    resp = await client.patch(
        f"/api/v1/tenant-memory/concepts/{seeded_concept.id}",
        json={"review_state": "garbage"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_delete_is_soft(client, admin_user, db, seeded_concept):
    _, headers = admin_user
    resp = await client.delete(f"/api/v1/tenant-memory/concepts/{seeded_concept.id}", headers=headers)
    assert resp.status_code == 204, resp.text

    # Row still exists, review_state flipped to rejected (NOT deleted).
    row = (
        await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.id == seeded_concept.id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.review_state == "rejected"

    # And it surfaces under the rejected filter.
    g = await client.get("/api/v1/tenant-memory?review_state=rejected", headers=headers)
    assert any(c["id"] == str(seeded_concept.id) for c in g.json()["concepts"])


async def test_delete_member_forbidden(client, member_user):
    _, headers = member_user
    resp = await client.delete(f"/api/v1/tenant-memory/concepts/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 403, resp.text


async def test_delete_cross_tenant_404(client, admin_user, admin_user_b, db):
    _, headers_a = admin_user
    user_b, _ = admin_user_b
    theirs = await _seed_concept(db, user_b.tenant_id, name="B Concept")

    resp = await client.delete(f"/api/v1/tenant-memory/concepts/{theirs.id}", headers=headers_a)
    assert resp.status_code == 404, resp.text
