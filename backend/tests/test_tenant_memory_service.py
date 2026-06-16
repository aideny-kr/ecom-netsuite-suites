"""Unit tests for the tenant memory CRUD service (flush-not-commit, tenant-scoped).

Mirrors the learned_rule_service contract: every query carries
`.where(tenant_id == ...)` (defense-in-depth on top of RLS), mutations flush
(the endpoint commits), and soft-delete flips review_state (never db.delete).
"""

import uuid

import pytest_asyncio

from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_link import TenantMemoryLink
from app.services import tenant_memory_service as svc
from tests.conftest import create_test_tenant, create_test_user


@pytest_asyncio.fixture
async def tenant_with_user(db):
    tenant = await create_test_tenant(db, name="Mem Corp", slug=f"mem-{uuid.uuid4().hex[:6]}")
    user, _ = await create_test_user(db, tenant, role_name="admin")
    return tenant, user


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


async def test_list_concepts_scoped_to_tenant(db, tenant_with_user):
    tenant, _ = tenant_with_user
    other = await create_test_tenant(db, name="Other", slug=f"oth-{uuid.uuid4().hex[:6]}")
    mine = await _seed_concept(db, tenant.id, name="Mine")
    await _seed_concept(db, other.id, name="Theirs")

    rows = await svc.list_concepts(db, tenant.id)
    ids = {c.id for c in rows}
    assert mine.id in ids
    assert all(c.tenant_id == tenant.id for c in rows)


async def test_list_concepts_filters_review_state(db, tenant_with_user):
    tenant, _ = tenant_with_user
    pending = await _seed_concept(db, tenant.id, name="P", review_state="pending")
    confirmed = await _seed_concept(db, tenant.id, name="C", review_state="confirmed")

    rows = await svc.list_concepts(db, tenant.id, review_state="confirmed")
    ids = {c.id for c in rows}
    assert confirmed.id in ids
    assert pending.id not in ids


async def test_get_concept_cross_tenant_is_none(db, tenant_with_user):
    tenant, _ = tenant_with_user
    other = await create_test_tenant(db, name="Other2", slug=f"oth2-{uuid.uuid4().hex[:6]}")
    theirs = await _seed_concept(db, other.id, name="Theirs")

    # Asking for another tenant's concept id under my tenant scope → None.
    got = await svc.get_concept(db, tenant.id, theirs.id)
    assert got is None


async def test_update_concept_sets_confirmed_by(db, tenant_with_user):
    tenant, user = tenant_with_user
    c = await _seed_concept(db, tenant.id, review_state="pending")

    updated = await svc.update_concept(db, tenant.id, c.id, review_state="confirmed", confirmed_by=user.id)
    assert updated is not None
    assert updated.review_state == "confirmed"
    assert updated.confirmed_by == user.id


async def test_update_concept_patches_fields(db, tenant_with_user):
    tenant, _ = tenant_with_user
    c = await _seed_concept(db, tenant.id, name="Old", summary="old summary")

    updated = await svc.update_concept(db, tenant.id, c.id, name="New", summary="new summary")
    assert updated.name == "New"
    assert updated.summary == "new summary"


async def test_editing_confirmed_text_deconfirms(db, tenant_with_user):
    """Editing the name/summary of a confirmed concept (without re-confirming in the
    same call) resets it to pending + clears confirmed_by — the edited authoritative
    text must be re-vetted before the read-loop injects it again."""
    tenant, user = tenant_with_user
    c = await _seed_concept(db, tenant.id, review_state="confirmed")
    c.confirmed_by = user.id
    await db.flush()

    updated = await svc.update_concept(db, tenant.id, c.id, summary="silently changed")
    assert updated.review_state == "pending"
    assert updated.confirmed_by is None


async def test_reconfirming_with_edit_stays_confirmed(db, tenant_with_user):
    """Passing review_state='confirmed' alongside an edit is a deliberate re-confirm
    of the new text and is honored (stays confirmed, re-stamps confirmed_by)."""
    tenant, user = tenant_with_user
    c = await _seed_concept(db, tenant.id, review_state="confirmed")

    updated = await svc.update_concept(
        db, tenant.id, c.id, summary="new vetted text", review_state="confirmed", confirmed_by=user.id
    )
    assert updated.review_state == "confirmed"
    assert updated.confirmed_by == user.id


async def test_soft_reject_is_not_delete(db, tenant_with_user):
    tenant, _ = tenant_with_user
    c = await _seed_concept(db, tenant.id, review_state="pending")

    ok = await svc.soft_reject_concept(db, tenant.id, c.id)
    assert ok is True

    # Row still present, review_state flipped to rejected.
    still = await svc.get_concept(db, tenant.id, c.id)
    assert still is not None
    assert still.review_state == "rejected"


async def test_soft_reject_cross_tenant_returns_false(db, tenant_with_user):
    tenant, _ = tenant_with_user
    other = await create_test_tenant(db, name="Other3", slug=f"oth3-{uuid.uuid4().hex[:6]}")
    theirs = await _seed_concept(db, other.id)
    ok = await svc.soft_reject_concept(db, tenant.id, theirs.id)
    assert ok is False


async def test_get_concept_links(db, tenant_with_user):
    tenant, _ = tenant_with_user
    c = await _seed_concept(db, tenant.id)
    link = TenantMemoryLink(
        tenant_id=tenant.id,
        concept_id=c.id,
        source_table="tenant_query_patterns",
        source_id=uuid.uuid4(),
    )
    db.add(link)
    await db.flush()

    links = await svc.get_concept_links(db, tenant.id, c.id)
    assert len(links) == 1
    assert links[0].concept_id == c.id
