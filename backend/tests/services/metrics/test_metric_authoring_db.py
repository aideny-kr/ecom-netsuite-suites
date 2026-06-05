# backend/tests/services/metrics/test_metric_authoring_db.py
"""DB-backed author-time validation: (a) leaf-existence over tenant ∪ SYSTEM.

Isolation contract (why per-test-unique leaf keys, not a global DELETE):
    Migration 080 seeds COMMITTED, ACTIVE, SYSTEM-tenant leaf rows (net_income,
    gross_revenue, net_margin, ...). The original version of these tests masked those
    committed rows with a transaction-local ``DELETE FROM metric_definitions`` (rolled
    back by the ``db`` fixture) and then re-seeded the SAME well-known keys under the
    shared SYSTEM tenant. That made each test's catalog depend on the global DELETE
    landing — and under READ COMMITTED a concurrent / rollback-lagged sibling
    transaction still sees the committed ACTIVE ``net_income``. The F4(a)
    "reject a non-active leaf" assertion then intermittently saw net_income as active
    and DID NOT RAISE (~1-in-3 under load right after docker startup).

    Fix: each test mints a unique per-test key suffix (``_kp``) and seeds its leaves /
    expression under keys that contain it. A unique key can NEVER collide with a
    committed migration-080 row OR a sibling test's rows on UNIQUE(tenant_id, key), so
    ``validate_leaves_exist`` (which filters by ``key IN depends_on``) resolves ONLY
    this test's own rows. No global DELETE, no cross-test / cross-transaction leakage —
    the catalog each test sees is deterministic regardless of sibling timing.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_authoring import AuthoringError, create_metric, validate_leaves_exist


def _kp() -> str:
    """A per-test key prefix unique across tests AND across the committed migration-080
    catalog, so each test's leaf keys resolve to its OWN rows only (no global DELETE,
    no shared-SYSTEM-key collision, no cross-test leakage)."""
    return f"t_{uuid.uuid4().hex[:12]}_"


async def _ensure_system_tenant(db):
    """Ensure the synthetic SYSTEM tenant parent row exists (SYSTEM-default leaf rows FK
    to tenants.id). Idempotent; rolled back per test by the ``db`` fixture.

    NOTE: deliberately does NOT ``DELETE FROM metric_definitions``. Tests seed under
    UNIQUE per-test keys, so the committed migration-080 catalog is irrelevant to them
    and the prior global-DELETE-then-rollback isolation race is removed entirely.
    """
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()


def _leaf(tenant_id, key: str, *, status: str = "active") -> MetricDefinition:
    return MetricDefinition(
        tenant_id=tenant_id,
        key=key,
        display_name=key,
        definition="x",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
        params_schema={},
        status=status,
        version=1,
    )


async def test_create_expression_metric_rejects_phantom_leaves(db, tenant_a, monkeypatch):
    """REAL anti-hallucination invariant (major #8). validate_definition proves
    depends_on matches the expression's references, but NOTHING checks the leaves
    actually exist. Authoring net_margin = a / b when NEITHER leaf is seeded persists a
    blessed-but-un-computable metric: the catalog advertises a named metric that can
    only ever resolve to missing_dependency.

    A real DB with the SYSTEM tenant present and the leaves ABSENT must make
    create_metric (via the DB-aware leaf check) raise AuthoringError → the API 422s.
    The prior create_metric inserted the row with zero leaf awareness, so this
    genuinely fails before the fix.

    Leaf keys are per-test-unique, so "absent" is guaranteed independent of the
    committed migration-080 catalog and of sibling tests — no global DELETE needed."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    leaf_a, leaf_b = f"{p}net_income", f"{p}gross_revenue"
    payload = {
        "key": f"{p}net_margin",
        "display_name": "Net Margin",
        "definition": "x",
        "unit": "percent",
        "source_kind": "expression",
        "expression": f"{leaf_a} / {leaf_b}",
        "depends_on": [leaf_a, leaf_b],  # neither seeded — and unique, so truly absent
    }
    with pytest.raises(AuthoringError):
        await create_metric(db, tenant_id=tenant_a.id, payload=payload)

    # And nothing was persisted (fail-loud, not fail-then-row).
    rows = (await db.execute(select(MetricDefinition).where(MetricDefinition.key == f"{p}net_margin"))).scalars().all()
    assert rows == []


async def test_create_expression_metric_accepts_existing_leaves(db, tenant_a, monkeypatch):
    """The same authoring SUCCEEDS once the two leaves are seeded (one as a SYSTEM
    default, one as a tenant row) — proving the check resolves leaves over tenant ∪
    SYSTEM, not just one scope, and does not over-reject valid metrics.

    Both leaves use this test's unique key prefix, so the tenant ∪ SYSTEM resolution is
    exercised against THIS test's own two rows — not whatever the committed catalog or a
    sibling test happens to hold."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    leaf_sys, leaf_tenant = f"{p}net_income", f"{p}gross_revenue"
    # One leaf seeded under SYSTEM, one under the tenant — so a pass PROVES the check
    # unions both scopes (neither scope alone has both keys).
    db.add(_leaf(SYSTEM_TENANT_ID, leaf_sys, status="active"))
    db.add(_leaf(tenant_a.id, leaf_tenant, status="active"))
    await db.flush()

    metric = await create_metric(
        db,
        tenant_id=tenant_a.id,
        payload={
            "key": f"{p}net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": f"{leaf_sys} / {leaf_tenant}",
            "depends_on": [leaf_sys, leaf_tenant],
        },
    )
    await db.flush()
    assert metric.key == f"{p}net_margin"
    assert metric.tenant_id == tenant_a.id


async def test_create_expression_metric_rejects_non_active_leaf(db, tenant_a, monkeypatch):
    """REAL author/compute-consistency invariant (F4 (a)). compute's leaf resolver
    (resolve_metric_by_key) filters status == 'active' — a leaf that exists but sits
    in needs_review/draft/deprecated resolves to None at compute and yields
    missing_dependency. validate_leaves_exist must use the SAME active-only lens so
    author-time leaf presence matches compute-time resolution.

    Here both leaves EXIST but one is needs_review. The prior validate_leaves_exist
    SELECT had no status filter, so it counted the non-active leaf as present and
    AUTHORED the expression — a blessed metric that can only ever resolve to
    missing_dependency at compute. With the active-only filter, author-time rejects it
    (→ 422), matching compute. Pre-fix this PASSES authoring (the bug); post-fix it
    raises AuthoringError.

    DETERMINISM: the non-active leaf uses this test's UNIQUE key. Previously the leaf
    was the well-known ``net_income`` re-seeded as needs_review AFTER a global
    ``DELETE FROM metric_definitions`` — but migration 080 ships a COMMITTED, ACTIVE
    ``net_income`` SYSTEM row. Under READ COMMITTED a concurrent / rollback-lagged
    sibling transaction still saw that committed ACTIVE row, so the leaf looked active
    and the reject DID NOT FIRE (~1-in-3 flake at the old line 167). A unique key cannot
    collide with the committed catalog, so the leaf this test seeds — needs_review — is
    the ONLY row matching it. The reject is now driven purely by THIS test's own row."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    leaf_inactive, leaf_active = f"{p}net_income", f"{p}gross_revenue"
    # Both leaves are present in the catalog, but leaf_inactive is NOT active — exactly
    # what compute's resolver would skip over. Unique keys → no committed ACTIVE namesake
    # can leak in and falsely satisfy the leaf.
    db.add(_leaf(SYSTEM_TENANT_ID, leaf_inactive, status="needs_review"))
    db.add(_leaf(SYSTEM_TENANT_ID, leaf_active, status="active"))
    await db.flush()

    with pytest.raises(AuthoringError):
        await create_metric(
            db,
            tenant_id=tenant_a.id,
            payload={
                "key": f"{p}net_margin",
                "display_name": "Net Margin",
                "definition": "x",
                "unit": "percent",
                "source_kind": "expression",
                "expression": f"{leaf_inactive} / {leaf_active}",
                "depends_on": [leaf_inactive, leaf_active],
            },
        )

    # And nothing was persisted (fail-loud, not fail-then-row).
    rows = (await db.execute(select(MetricDefinition).where(MetricDefinition.key == f"{p}net_margin"))).scalars().all()
    assert rows == []


async def test_create_expression_metric_rejects_when_only_committed_namesake_is_active(db, tenant_a, monkeypatch):
    """Regression guard for the exact flake mechanism (F4 (a) under cross-transaction
    visibility). Migration 080 ships a COMMITTED, ACTIVE ``net_income`` SYSTEM row. This
    test seeds — WITHOUT deleting that committed catalog — its OWN needs_review leaf
    under a unique key, then authors an expression over it.

    The reject MUST fire because THIS test's leaf is needs_review. If the
    validate_leaves_exist query were keyed on the well-known ``net_income`` (the prior
    design) instead of a unique key, the committed ACTIVE namesake — which is present
    here precisely because we did NOT run the global DELETE — would satisfy the leaf and
    authoring would WRONGLY succeed. Unique-key seeding makes the reject deterministic
    even with the full committed catalog visible. This is the test that would have
    caught the flake deterministically."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    # Sanity: the committed migration-080 active net_income IS visible in this tx (we
    # never deleted it) — this is the leakage source the old shared-key seeding fell to.
    committed_active = (
        await db.execute(
            select(MetricDefinition.status).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "net_income",
            )
        )
    ).scalar_one_or_none()
    # Either the catalog is seeded (status active) or not present on a bare DB; both are
    # fine — the point is our unique-key leaf is what drives the assertion, not this row.
    assert committed_active in ("active", None)

    p = _kp()
    leaf_inactive, leaf_active = f"{p}net_income", f"{p}gross_revenue"
    db.add(_leaf(SYSTEM_TENANT_ID, leaf_inactive, status="needs_review"))
    db.add(_leaf(SYSTEM_TENANT_ID, leaf_active, status="active"))
    await db.flush()

    with pytest.raises(AuthoringError):
        await create_metric(
            db,
            tenant_id=tenant_a.id,
            payload={
                "key": f"{p}net_margin",
                "display_name": "Net Margin",
                "definition": "x",
                "unit": "percent",
                "source_kind": "expression",
                "expression": f"{leaf_inactive} / {leaf_active}",
                "depends_on": [leaf_inactive, leaf_active],
            },
        )


async def test_validate_leaves_exist_is_noop_for_query_backed(db, tenant_a):
    """A query-backed metric has no leaves; the DB-aware check must be a no-op
    (never spuriously reject a single-source metric)."""
    await _ensure_system_tenant(db)
    await validate_leaves_exist(
        db,
        tenant_id=tenant_a.id,
        d={
            "key": f"{_kp()}gross_revenue",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 1", "dialect": "suiteql"},
            "depends_on": None,
        },
    )
