# backend/tests/services/metrics/test_metric_catalog_seeder.py
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_catalog_seeder import seed_system_metrics


async def _ensure_system_tenant(db):
    # SYSTEM-default metric rows FK to tenants.id; seed the canonical SYSTEM tenant
    # parent row (rolled back per test by the db fixture) so the insert is valid.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()


async def test_seed_is_idempotent_and_system_scoped(db):
    await _ensure_system_tenant(db)
    n1 = await seed_system_metrics(db)
    await db.flush()
    n2 = await seed_system_metrics(db)  # re-seed
    await db.flush()
    assert n1 == n2 and n1 >= 8
    total = (
        await db.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()
    assert total == n1  # idempotent: no duplicates


async def test_seeder_is_self_sufficient_when_system_tenant_absent(db):
    """REAL invariant (blocker #3): on a FRESH DB the SYSTEM tenant row does NOT
    exist, so the seeder's INSERT INTO metric_definitions FKs to a missing parent
    and raises ForeignKeyViolationError. The prior test masked this by pre-inserting
    the SYSTEM tenant in test code (vacuous). Here we DELETE the SYSTEM tenant + its
    metric rows, then run the seeder WITHOUT seeding the tenant ourselves — the
    seeder must upsert the SYSTEM tenant first and seed 9 rows with no FK violation."""
    # Tear down to mimic a fresh DB: SYSTEM metric rows then the SYSTEM tenant row.
    await db.execute(delete(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID))
    await db.execute(delete(Tenant).where(Tenant.id == SYSTEM_TENANT_ID))
    await db.flush()
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() is None  # genuinely absent — the seeder is on its own

    # No _ensure_system_tenant() here — the seeder itself must provision the parent.
    n = await seed_system_metrics(db)
    await db.flush()

    assert n == 9
    # The seeder created the SYSTEM tenant parent row (defense-in-depth upsert).
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() == SYSTEM_TENANT_ID
    total = (
        await db.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()
    assert total == 9


async def test_concurrent_reseed_uses_upsert_not_unique_violation(db):
    """R3#25 — two concurrent seeders must converge via upsert, not race into a
    UNIQUE(tenant_id, key) violation.

    Within a single session (shared uncommitted state) the second seed() call
    must not raise IntegrityError / UniqueViolationError. With the old
    delete-then-insert approach the second DELETE removes what the first inserted
    and the second INSERT re-adds — that's safe within a transaction but breaks
    if two sessions race at the COMMIT boundary.  The ON CONFLICT DO UPDATE
    upsert fixes that by making the second call a no-op update, never a new row.

    This test simulates the within-transaction half of that invariant:
    seed → flush → seed again → flush → no exception + stable count.
    The across-commit half is already covered by
    test_metric_seed_commit_idempotency.py (F5).
    """
    from sqlalchemy.exc import IntegrityError

    await _ensure_system_tenant(db)

    n1 = await seed_system_metrics(db)
    await db.flush()

    # A second seed in the same uncommitted session must NOT raise.
    try:
        n2 = await seed_system_metrics(db)
        await db.flush()
    except IntegrityError as exc:
        pytest.fail(f"seeder raised IntegrityError on second call — ON CONFLICT upsert is required (R3#25): {exc}")

    assert n1 == n2, "seeder must return the same count on every call"
    total = (
        await db.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()
    assert total == n1, f"upsert must not double rows: expected {n1}, got {total}"


async def test_placeholder_defaults_are_draft_not_active(db):
    """D3: SELECT 0 placeholder rows must seed as draft, not active.
    §12.2: all seeded rows must carry a non-null 1536-d embedding."""
    from sqlalchemy import select

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.services.metrics.metric_catalog_seeder import seed_system_metrics

    await seed_system_metrics(db)
    rows = (
        (await db.execute(select(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)))
        .scalars()
        .all()
    )
    placeholders = [r for r in rows if r.source_kind in ("suiteql", "bigquery")]
    assert placeholders, "expected seeded query-backed defaults"
    assert all(r.status == "draft" for r in placeholders), "D3: SELECT 0 placeholders must seed draft, never active"
    assert all(r.intent_embedding is not None for r in placeholders), "§12.2: seeded rows need non-null embeddings"


async def test_seeder_sets_system_tenant_context_before_writes(db):
    """Migration 081 added FORCE ROW LEVEL SECURITY to metric_definitions.
    On Supabase the app role is the table OWNER but NOT BYPASSRLS, so FORCE RLS
    applies. Without an active tenant context, get_current_tenant_id() throws
    "unrecognized configuration parameter 'app.current_tenant_id'" on any INSERT.

    This test records a SHARED, chronologically-ordered event log: the
    set_tenant_context spy appends a ("set_context", tenant_id) marker and the
    db.execute spy appends an ("execute", stmt) marker into the SAME list. It then
    asserts the set_context marker physically precedes the FIRST execute marker —
    a genuine ordering assertion, not just "set_tenant_context was called".

    NOTE: Local docker runs postgres as a BYPASSRLS superuser, so the RLS
    enforcement path itself cannot be exercised here. This spy test validates
    the call-ordering contract that the production Supabase path requires.
    """
    import app.services.metrics.metric_catalog_seeder as seeder_mod

    # Single shared call-order log so we can prove set_context happens BEFORE
    # the first write, not merely that both happened.
    events: list[tuple[str, object]] = []

    original_execute = db.execute

    async def spy_execute(stmt, *args, **kwargs):
        events.append(("execute", stmt))
        return await original_execute(stmt, *args, **kwargs)

    async def spy_set_tenant_context(session, tenant_id):
        events.append(("set_context", tenant_id))

    db.execute = spy_execute

    with patch.object(seeder_mod, "set_tenant_context", spy_set_tenant_context):
        await seed_system_metrics(db)

    # Restore db.execute so the db fixture rollback still works.
    db.execute = original_execute

    set_context_idxs = [i for i, (kind, _) in enumerate(events) if kind == "set_context"]
    execute_idxs = [i for i, (kind, _) in enumerate(events) if kind == "execute"]

    # set_tenant_context must have been called at least once with SYSTEM_TENANT_ID.
    assert set_context_idxs, "set_tenant_context was never called — FORCE RLS will throw on Supabase"
    set_context_tenants = [events[i][1] for i in set_context_idxs]
    assert str(SYSTEM_TENANT_ID) in [str(t) for t in set_context_tenants], (
        f"set_tenant_context must be called with SYSTEM_TENANT_ID ({SYSTEM_TENANT_ID}), "
        f"got calls: {set_context_tenants}"
    )

    # The seeder must run at least one write (ensure_system_tenant INSERT + the
    # metric upserts) — otherwise there is nothing for the ordering to guard.
    assert execute_idxs, "seeder must execute at least one statement (the metric upserts)"

    # ORDERING INVARIANT (the actual contract): set_tenant_context fires BEFORE the
    # first db.execute. On a fresh GUC, any write before SET LOCAL app.current_tenant_id
    # throws "unrecognized configuration parameter" under FORCE RLS on Supabase.
    first_set_context = set_context_idxs[0]
    first_execute = execute_idxs[0]
    assert first_set_context < first_execute, (
        "set_tenant_context must be called BEFORE the first db.execute write — "
        f"got set_context at index {first_set_context}, first execute at index {first_execute}. "
        f"Event order: {[kind for kind, _ in events]}"
    )


async def test_reseed_preserves_superadmin_edited_canonical_metric(db):
    """B2: reseed must NOT clobber a superadmin-authored SYSTEM metric.

    Scenario: superadmin edits the canonical 'cash' metric via PUT /metrics/system/{id},
    stamping provenance.author='superadmin', a real GL query, and status='active'.
    The nightly Beat reseed must leave the row untouched (WHERE predicate on existing
    provenance.author == 'system_seed' is FALSE → no update, existing row preserved).
    """
    from sqlalchemy import update

    await _ensure_system_tenant(db)

    # Step 1: seed once — all rows are system_seed-owned
    await seed_system_metrics(db)
    await db.flush()

    # Step 2: simulate a superadmin edit on the 'cash' row
    await db.execute(
        update(MetricDefinition)
        .where(
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
            MetricDefinition.key == "cash",
        )
        .values(
            blessed_spec={"query": "SELECT SUM(amount) FROM transaction", "dialect": "suiteql"},
            status="active",
            provenance={"author": "superadmin"},
        )
    )
    await db.flush()

    # Step 3: reseed — must NOT overwrite the superadmin row
    await seed_system_metrics(db)
    await db.flush()

    row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "cash",
            )
        )
    ).scalar_one()

    assert row.blessed_spec == {"query": "SELECT SUM(amount) FROM transaction", "dialect": "suiteql"}, (
        "B2: reseed clobbered superadmin's blessed_spec back to SELECT 0 draft"
    )
    assert row.status == "active", "B2: reseed reverted status to 'draft'"
    assert row.provenance["author"] == "superadmin", "B2: reseed overwrote provenance.author back to 'system_seed'"


async def test_reseed_still_refreshes_seeder_owned_rows(db):
    """B2 complement: conditional upsert must still update rows that remain seeder-owned.

    Scenario: a seeder-owned row is accidentally corrupted (e.g. display_name='STALE').
    The nightly reseed should detect provenance.author=='system_seed' and restore it.
    """
    from sqlalchemy import update

    await _ensure_system_tenant(db)

    # Step 1: seed once
    await seed_system_metrics(db)
    await db.flush()

    # Step 2: corrupt a seeder-owned row (provenance.author still 'system_seed')
    await db.execute(
        update(MetricDefinition)
        .where(
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
            MetricDefinition.key == "cash",
        )
        .values(display_name="STALE")
    )
    await db.flush()

    # Verify corruption is in place
    stale_row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "cash",
            )
        )
    ).scalar_one()
    assert stale_row.display_name == "STALE"
    assert stale_row.provenance["author"] == "system_seed"

    # Step 3: reseed — must refresh the seeder-owned row
    await seed_system_metrics(db)
    await db.flush()
    # Expire the identity map so the following SELECT hits the DB, not the
    # in-session cache (pg_insert upserts bypass ORM tracking).
    db.expire_all()

    refreshed_row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "cash",
            )
        )
    ).scalar_one()

    assert refreshed_row.display_name == "Cash", (
        "B2 complement: conditional upsert failed to refresh a seeder-owned row — "
        f"display_name is still '{refreshed_row.display_name}'"
    )
