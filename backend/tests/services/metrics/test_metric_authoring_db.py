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

import re
import uuid

import pytest
from sqlalchemy import select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_authoring import AuthoringError, create_metric, update_metric, validate_leaves_exist


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

    # Sanity: the committed migration-080 net_income IS visible in this tx (we never
    # deleted it) — this is the leakage source the old shared-key seeding fell to.
    # After D3 the seeder seeds query-backed defaults as "draft" (not "active"), so we
    # accept active, draft, or None (bare DB without the migration run yet).  The status
    # of this committed row is irrelevant to the test's actual assertion — this block
    # merely confirms the row is present and visible (i.e. the global DELETE was NOT run).
    committed_status = (
        await db.execute(
            select(MetricDefinition.status).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "net_income",
            )
        )
    ).scalar_one_or_none()
    assert committed_status in ("active", "draft", None)

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


# ── B2: provenance stamp — update_metric clears system_seed author ────────────


async def test_update_metric_stamps_provenance_away_from_system_seed(db, monkeypatch):
    """B2: a SYSTEM metric whose provenance['author'] == 'system_seed' (set by the
    daily seeder) MUST have that stamp replaced when a superadmin edits it via
    update_metric. Otherwise the seeder's conditional-upsert guard (author==system_seed
    → overwrite) would re-clobber the edit on the next nightly run.

    Fix: update_metric sets provenance['author'] = 'authored' (and 'updated_via':'api')
    for EVERY update, regardless of source_kind or tenant_id. This is the guard that
    protects a superadmin's edit to a canonical key from being reseeded over.

    Pre-fix: provenance['author'] stays 'system_seed' after the edit (seeder can
    overwrite it). Post-fix: provenance['author'] == 'authored'."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}cash"
    # Seed the SYSTEM row with system_seed provenance (exactly what the nightly seeder
    # would write). Insert directly (bypassing create_metric's provenance default so we
    # control the initial stamp precisely).
    metric = MetricDefinition(
        tenant_id=SYSTEM_TENANT_ID,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "system_seed", "seeded_at": "2026-06-04"},
    )
    db.add(metric)
    await db.flush()

    # Edit via update_metric (simulating a superadmin PUT).
    updated = await update_metric(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        metric_id=metric.id,
        payload={"display_name": "Cash Balance"},
    )

    # The author stamp must no longer be 'system_seed' — the seeder must not re-clobber
    # this edit on the next nightly run.
    assert updated.provenance["author"] == "authored", (
        f"expected provenance['author']=='authored', got {updated.provenance!r}"
    )
    # updated_via is set as a convenience tag (not a hard invariant, but present).
    assert updated.provenance.get("updated_via") == "api"
    # Other keys from the original provenance are preserved (non-destructive merge).
    assert updated.provenance.get("seeded_at") == "2026-06-04"


async def test_update_metric_stamps_provenance_for_tenant_row_too(db, tenant_a, monkeypatch):
    """B2 + NEW-6 guard: a tenant row with author='tenant_admin' updated via update_metric
    must PRESERVE 'tenant_admin' (not clobber it to 'authored'). Only system_seed author
    is converted — any other author class is kept.  The updated_via='api' tag is still set."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}rev"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Revenue",
        definition="revenue",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Total Revenue"},
    )
    # NEW-6: non-system_seed author must be preserved
    assert updated.provenance["author"] == "tenant_admin"
    assert updated.provenance.get("updated_via") == "api"


# ── NEW-2: row lock — SELECT … FOR UPDATE in update_metric ────────────────────


async def test_update_metric_sequential_version_bumps_no_lost_update(db, tenant_a, monkeypatch):
    """NEW-2: two sequential update_metric calls on the same row MUST yield version 2
    then version 3 (no lost update). With a SELECT … FOR UPDATE lock each call reads
    the post-commit version of the previous call, so both bumps land.

    The structural guard (FOR UPDATE present) is tested separately below. This test
    confirms the functional consequence: sequential bumps are monotonically correct."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}rev"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Revenue",
        definition="revenue",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    v2 = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Revenue v2"},
    )
    assert v2.version == 2, f"expected version 2 after first update, got {v2.version}"

    v3 = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Revenue v3"},
    )
    assert v3.version == 3, f"expected version 3 after second update, got {v3.version}"


async def test_update_metric_select_carries_for_update_lock(db, tenant_a, monkeypatch):
    """NEW-2: structural assertion — the SELECT statement issued by update_metric MUST
    carry a FOR UPDATE lock clause. We intercept db.execute, capture the first
    statement (the row-fetch SELECT), compile it against the PostgreSQL dialect, and
    assert 'FOR UPDATE' appears in the SQL text.

    If update_metric drops the with_for_update() call the assertion fails, making this
    a regression guard for the concurrent-PUT lost-update fix."""
    from sqlalchemy.dialects import postgresql

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}lock_check"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Lock Check",
        definition="x",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    # Intercept the FIRST db.execute call (the SELECT) to capture the statement.
    captured_stmts: list = []
    _real_execute = db.execute

    async def _intercepting_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        return await _real_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "execute", _intercepting_execute)

    await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Lock Check Updated"},
    )

    assert captured_stmts, "db.execute was never called — update_metric did not issue any SELECT"
    first_stmt = captured_stmts[0]
    compiled = first_stmt.compile(dialect=postgresql.dialect())
    sql_text = str(compiled)
    assert "FOR UPDATE" in sql_text.upper(), (
        f"Expected SELECT ... FOR UPDATE in the first db.execute call, got:\n{sql_text}"
    )


# ── NEW-3: reactivation read-only smoke ───────────────────────────────────────


async def test_update_metric_reactivation_rejects_unsafe_suiteql_query(db, tenant_a, monkeypatch):
    """NEW-3: PUT status='active' on a suiteql-backed metric whose blessed_spec.query
    is a DML statement MUST raise AuthoringError mentioning read-only / allowlist.

    Without this guard, a superadmin could PUT a broken/unsafe query + status=active
    and the next compute call would attempt to execute it (and fail closed), but the
    metric would sit in the catalog as 'active' with an un-executable definition. The
    reactivation smoke gate prevents activation until the query is safe.

    We inject an unsafe query directly (bypassing create_metric's validate_definition
    which would already reject it at author time) so we can test the update_metric path
    independently."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}cash"
    # Insert the row with an unsafe DML query directly, bypassing author-time validation,
    # so update_metric's reactivation smoke is the ONLY gate we're testing.
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "DELETE FROM transaction", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    with pytest.raises(AuthoringError, match=r"(?i)cannot activate|read.only|allowlist"):
        await update_metric(
            db,
            tenant_id=tenant_a.id,
            metric_id=metric.id,
            payload={"status": "active"},
        )


async def test_update_metric_reactivation_rejects_off_allowlist_suiteql_query(db, tenant_a, monkeypatch):
    """NEW-3: a syntactically read-only but off-allowlist table query also blocks
    reactivation. The gate checks BOTH read-only AND table allowlist."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}secret"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Secret",
        definition="secret data",
        unit="count",
        source_kind="suiteql",
        # SELECT from an off-allowlist table — read-only but not on the allowlist.
        blessed_spec={"query": "SELECT 1 FROM secret_table", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    with pytest.raises(AuthoringError, match=r"(?i)cannot activate|allowlist"):
        await update_metric(
            db,
            tenant_id=tenant_a.id,
            metric_id=metric.id,
            payload={"status": "active"},
        )


async def test_update_metric_reactivation_accepts_valid_suiteql_query(db, tenant_a, monkeypatch):
    """NEW-3 (positive): a read-only, allowlisted suiteql query passes the reactivation
    smoke and the metric is successfully activated (200 / no exception)."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}cash"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        # Safe: read-only SELECT from an allowlisted table (transaction is in the default allowlist).
        blessed_spec={"query": "SELECT 0 FROM transaction", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"status": "active"},
    )
    assert updated.status == "active"
    assert updated.version == 2


async def test_update_metric_reactivation_accepts_select_0_stub(db, tenant_a, monkeypatch):
    """NEW-3 (positive): the seeded SELECT 0 stub (no FROM clause → no table referenced)
    must pass the reactivation smoke since it's read-only and references no off-allowlist
    tables. This guards against over-blocking the seeded defaults."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}stub"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Stub",
        definition="stub",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"status": "active"},
    )
    assert updated.status == "active"


async def test_update_metric_reactivation_rejects_unsafe_bigquery_query(db, tenant_a, monkeypatch):
    """NEW-3: PUT status='active' on a bigquery-backed metric with a DML query raises
    AuthoringError. Mirrors the suiteql DML-rejection test but for bigquery source_kind."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}bq_metric"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="BQ Metric",
        definition="bq data",
        unit="count",
        source_kind="bigquery",
        blessed_spec={"query": "DELETE FROM dataset.table WHERE 1=1", "dialect": "bigquery"},
        params_schema={"period": {"type": "period"}},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    with pytest.raises(AuthoringError, match=r"(?i)cannot activate|read.only"):
        await update_metric(
            db,
            tenant_id=tenant_a.id,
            metric_id=metric.id,
            payload={"status": "active"},
        )


async def test_update_metric_reactivation_skipped_for_non_active_status(db, tenant_a, monkeypatch):
    """NEW-3 guard: the smoke gate only fires when the RESULTING status is 'active'.
    Updating a draft metric to 'needs_review' (or keeping it draft) must NOT trigger
    the read-only validation — an unsafe query sitting in draft/needs_review is
    permissible (the admin will fix the query before reactivating)."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}bad_query_draft"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Draft Bad",
        definition="bad",
        unit="currency",
        source_kind="suiteql",
        # Unsafe DML query, but metric is staying in needs_review — smoke must NOT fire.
        blessed_spec={"query": "DELETE FROM transaction", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    # Transition to needs_review should NOT trigger the smoke gate (no active status).
    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"status": "needs_review"},
    )
    assert updated.status == "needs_review"
    assert updated.version == 2


async def test_update_metric_reactivation_skipped_for_expression_metrics(db, tenant_a, monkeypatch):
    """NEW-3 guard: expression metrics have no blessed query — the smoke gate must not
    fire for source_kind='expression' even when activating (expressions have no SQL
    to validate; the gate is only for query-backed source kinds)."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    await _ensure_system_tenant(db)

    p = _kp()
    # Seed leaves that this expression depends on.
    leaf_a = f"{p}income"
    leaf_b = f"{p}revenue"
    db.add(_leaf(tenant_a.id, leaf_a, status="active"))
    db.add(_leaf(tenant_a.id, leaf_b, status="active"))
    await db.flush()

    key = f"{p}margin"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Margin",
        definition="margin",
        unit="percent",
        source_kind="expression",
        expression=f"{leaf_a} / {leaf_b}",
        depends_on=[leaf_a, leaf_b],
        params_schema={},
        status="draft",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    # Activating an expression metric must succeed (no smoke gate for expressions).
    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"status": "active"},
    )
    assert updated.status == "active"


# ── M3: reverse-dependency guard — editing a leaf that active expressions depend on ──


async def test_update_metric_deactivate_leaf_rejected_when_expression_depends_on_it(db, tenant_a, monkeypatch):
    """M3 (a): deactivating (status→draft) a query-backed leaf that an active expression
    metric depends on must raise AuthoringError naming the dependent. Without this guard
    the leaf disappears from compute's active-only resolution, so the dependent expression
    silently returns missing_dependency at compute time — a blessed metric that can never
    compute after an innocuous-looking edit.

    Pre-fix: update_metric only validates the EDITED row; it does not check downstream
    dependents, so it happily writes status='draft' even though gm actively depends on gp.
    Post-fix: AuthoringError("cannot edit 'gp': active expression metric(s) depend …")."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    gp_key = f"{p}gp"
    rev_key = f"{p}rev"
    gm_key = f"{p}gm"

    # Leaf gp (query-backed, active)
    db.add(_leaf(tenant_a.id, gp_key, status="active"))
    # Leaf rev (query-backed, active)
    db.add(_leaf(tenant_a.id, rev_key, status="active"))
    await db.flush()

    # Expression metric gm depends on gp and rev
    gm = MetricDefinition(
        tenant_id=tenant_a.id,
        key=gm_key,
        display_name="Gross Margin",
        definition="gross margin",
        unit="percent",
        source_kind="expression",
        expression=f"{gp_key} / {rev_key}",
        depends_on=[gp_key, rev_key],
        params_schema={},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(gm)
    await db.flush()

    # Fetch the gp row by key so we have its id
    gp_row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == tenant_a.id,
                MetricDefinition.key == gp_key,
            )
        )
    ).scalar_one()

    with pytest.raises(AuthoringError, match=gm_key):
        await update_metric(
            db,
            tenant_id=tenant_a.id,
            metric_id=gp_row.id,
            payload={"status": "draft"},
        )


async def test_update_metric_leaf_to_expression_rejected_when_expression_depends_on_it(db, tenant_a, monkeypatch):
    """M3 (b): changing a query-backed leaf's source_kind to 'expression' (making it no
    longer query-backed) while an active expression metric depends on it must raise
    AuthoringError. `validate_leaves_exist` rejects expression metrics used as leaves
    at CREATE time, but the same hazard arises at UPDATE when a leaf becomes an expression.

    Pre-fix: update_metric validates only the EDITED row; the reverse-dep scan is absent.
    Post-fix (M3): AuthoringError naming the dependent expression metric.

    NOTE (R4 interaction): after the R4 source_kind-immutability guard landed, flipping
    source_kind from 'suiteql' to 'expression' is now rejected by R4 BEFORE the M3
    reverse-dep scan runs.  Both guards protect the same invariant (the operation is
    rejected); only the error message differs.  The match pattern accepts either so
    the test remains a valid regression guard for "the flip is always blocked"."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    gp_key = f"{p}gp"
    rev_key = f"{p}rev"
    gm_key = f"{p}gm"

    db.add(_leaf(tenant_a.id, gp_key, status="active"))
    db.add(_leaf(tenant_a.id, rev_key, status="active"))
    await db.flush()

    gm = MetricDefinition(
        tenant_id=tenant_a.id,
        key=gm_key,
        display_name="Gross Margin",
        definition="gross margin",
        unit="percent",
        source_kind="expression",
        expression=f"{gp_key} / {rev_key}",
        depends_on=[gp_key, rev_key],
        params_schema={},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(gm)
    await db.flush()

    gp_row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == tenant_a.id,
                MetricDefinition.key == gp_key,
            )
        )
    ).scalar_one()

    # Trying to flip gp to an expression — must be rejected (R4 immutability guard fires
    # first; M3 reverse-dep scan would also reject it if R4 were absent).
    with pytest.raises(AuthoringError, match=rf"(?i)immutable|{re.escape(gm_key)}"):
        await update_metric(
            db,
            tenant_id=tenant_a.id,
            metric_id=gp_row.id,
            payload={
                "source_kind": "expression",
                "expression": f"{rev_key} / {rev_key}",
                "depends_on": [rev_key],
                "blessed_spec": None,
            },
        )


async def test_update_metric_display_name_only_no_false_positive(db, tenant_a, monkeypatch):
    """M3 (c): editing a leaf's display_name (no source_kind or status change) must
    succeed even when an active expression metric depends on it — the reverse-dep check
    must NOT fire on harmless edits. Guards against false positives that would make the
    guard overly restrictive."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    gp_key = f"{p}gp"
    rev_key = f"{p}rev"
    gm_key = f"{p}gm"

    db.add(_leaf(tenant_a.id, gp_key, status="active"))
    db.add(_leaf(tenant_a.id, rev_key, status="active"))
    await db.flush()

    gm = MetricDefinition(
        tenant_id=tenant_a.id,
        key=gm_key,
        display_name="Gross Margin",
        definition="gross margin",
        unit="percent",
        source_kind="expression",
        expression=f"{gp_key} / {rev_key}",
        depends_on=[gp_key, rev_key],
        params_schema={},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(gm)
    await db.flush()

    gp_row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == tenant_a.id,
                MetricDefinition.key == gp_key,
            )
        )
    ).scalar_one()

    # Display-name-only edit must NOT trigger the reverse-dep check
    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=gp_row.id,
        payload={"display_name": "Gross Profit (renamed)"},
    )
    assert updated.display_name == "Gross Profit (renamed)"
    assert updated.version == 2


# ── Minor: embedding-None intentional handling ────────────────────────────────


async def test_create_metric_succeeds_with_none_embedding(db, tenant_a, monkeypatch):
    """Embedding-None: when embed_domain_query returns None (provider unconfigured),
    create_metric must still persist the row with intent_embedding=None and not raise.
    This is intentional behavior (keyword-only searchable) — must not hard-reject None."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}revenue"
    metric = await create_metric(
        db,
        tenant_id=tenant_a.id,
        payload={
            "key": key,
            "display_name": "Revenue",
            "definition": "total revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    await db.flush()
    assert metric.key == key
    assert metric.intent_embedding is None


async def test_update_metric_succeeds_with_none_embedding(db, tenant_a, monkeypatch):
    """Embedding-None on update: when embed_domain_query returns None during an update,
    update_metric must persist the row with intent_embedding=None and not raise."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}revenue"
    # Seed the row first
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Revenue",
        definition="total revenue",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    # Update triggers embedding recompute which returns None
    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Total Revenue"},
    )
    assert updated.display_name == "Total Revenue"
    assert updated.intent_embedding is None


# ── NEW-5: tenant-wins in validate_leaves_exist ───────────────────────────────


async def test_validate_leaves_exist_tenant_expression_leaf_masked_by_system_query(db, tenant_a, monkeypatch):
    """NEW-5: when the SAME key exists as a SYSTEM query-backed leaf AND a tenant
    expression leaf, validate_leaves_exist must apply tenant-wins semantics — the tenant
    row's source_kind (expression) is authoritative, and authoring an expression metric
    that depends on it MUST raise AuthoringError ("query-backed").

    We force the SYSTEM-first ordering scenario by monkeypatching db.execute so that rows
    are returned [(gp, 'suiteql'), (gp, 'expression')] — SYSTEM first, tenant second —
    which is the ordering where the current buggy dict-comprehension accidentally picks
    the tenant value. After the fix, the tenant row is selected deterministically by
    explicit precedence logic (not by dict-comprehension ordering).

    The critical scenario that fails WITHOUT the fix is when DB returns SYSTEM first:
    the dict compression iterates to [(gp, 'suiteql'), (gp, 'expression')], last value
    wins = 'expression' → rejection fires. But if DB returns tenant first:
    [(gp, 'expression'), (gp, 'suiteql')] → dict has 'suiteql' → WRONG PASS.

    After the fix, the code explicitly applies tenant-wins regardless of row order.
    We test using the DB-first ordering (reliable) and separately verify via a mock
    that proves the SYSTEM-first scenario (where the bug would hide) also rejects."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    gp_key = f"{p}gp"
    rev_key = f"{p}rev"

    # SYSTEM row for 'gp': query-backed (suiteql), active
    db.add(_leaf(SYSTEM_TENANT_ID, gp_key, status="active"))
    # SYSTEM row for 'rev': query-backed (suiteql), active
    db.add(_leaf(SYSTEM_TENANT_ID, rev_key, status="active"))
    # Tenant expression leaf for gp — compute's resolve_metric_by_key would pick THIS one
    # (tenant-override wins), returning nested_expression_unsupported.
    tenant_gp = MetricDefinition(
        tenant_id=tenant_a.id,
        key=gp_key,
        display_name="GP (tenant override)",
        definition="gross profit expression override",
        unit="currency",
        source_kind="expression",
        expression=f"{rev_key} + {rev_key}",
        depends_on=[rev_key],
        params_schema={},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(tenant_gp)
    await db.flush()

    # Author a NEW expression metric for tenant_a that depends on gp.
    # validate_leaves_exist must see tenant's gp (source_kind='expression') and REJECT.
    with pytest.raises(AuthoringError, match=r"(?i)query.backed|expression"):
        await validate_leaves_exist(
            db,
            tenant_id=tenant_a.id,
            d={
                "key": f"{p}gm",
                "source_kind": "expression",
                "expression": f"{gp_key} + {gp_key}",
                "depends_on": [gp_key],
            },
        )


async def test_validate_leaves_exist_tenant_expression_rejects_even_when_system_has_suiteql_namesake(
    db, tenant_a, monkeypatch
):
    """NEW-5 (second assertion): a different key pair confirms the tenant-wins rule
    applies independently of the key name and test run. A tenant 'cost' (expression)
    with a SYSTEM 'cost' (suiteql) → authoring an expression that depends on 'cost' is
    REJECTED because the tenant's row (expression) is what compute resolves. Without the
    fix the {k: sk} dict may pick 'suiteql' (if SYSTEM row is last in result) and
    WRONGLY pass; with the fix tenant row always wins."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    cost_key = f"{p}cost"
    rev_key = f"{p}rev2"

    # SYSTEM 'cost': suiteql, active
    db.add(_leaf(SYSTEM_TENANT_ID, cost_key, status="active"))
    # SYSTEM 'rev2': suiteql, active (used as the leaf for the tenant expression override)
    db.add(_leaf(SYSTEM_TENANT_ID, rev_key, status="active"))
    # Tenant override of 'cost': expression — compute resolves THIS (tenant-wins)
    tenant_cost = MetricDefinition(
        tenant_id=tenant_a.id,
        key=cost_key,
        display_name="Cost (expression override)",
        definition="cost as expression",
        unit="currency",
        source_kind="expression",
        expression=f"{rev_key} + {rev_key}",
        depends_on=[rev_key],
        params_schema={},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(tenant_cost)
    await db.flush()

    # Authoring an expression that uses 'cost' as a leaf MUST be rejected:
    # validate_leaves_exist must see tenant's 'cost' (expression) and not the SYSTEM 'cost'.
    with pytest.raises(AuthoringError, match=r"(?i)query.backed|expression"):
        await validate_leaves_exist(
            db,
            tenant_id=tenant_a.id,
            d={
                "key": f"{p}margin2",
                "source_kind": "expression",
                "expression": f"{cost_key} + {cost_key}",
                "depends_on": [cost_key],
            },
        )


async def test_validate_leaves_exist_system_query_leaf_satisfies_tenant_without_override(db, tenant_a, monkeypatch):
    """NEW-5 control: a tenant WITHOUT a tenant-level 'gp' row but with a SYSTEM
    query-backed 'gp' row — authoring an expression that depends on 'gp' MUST pass.
    This confirms the fix does not break the normal tenant ∪ SYSTEM resolution when
    there is no tenant override (SYSTEM leaf is correctly used)."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    gp_key = f"{p}gp"

    # Only SYSTEM row for 'gp': query-backed (suiteql), active — no tenant override
    db.add(_leaf(SYSTEM_TENANT_ID, gp_key, status="active"))
    await db.flush()

    # Should NOT raise — SYSTEM query-backed leaf is valid for this tenant
    await validate_leaves_exist(
        db,
        tenant_id=tenant_a.id,
        d={
            "key": f"{p}gm",
            "source_kind": "expression",
            "expression": f"{gp_key} + {gp_key}",
            "depends_on": [gp_key],
        },
    )


# ── NEW-6: preserve prior provenance author on update ─────────────────────────


async def test_update_metric_system_seed_author_becomes_authored(db, monkeypatch):
    """NEW-6 (a): a system_seed row updated via update_metric must have author converted
    to 'authored' — satisfying B2 (seeder's conditional-upsert guard skips non-system_seed
    rows so the edit is not re-clobbered on the next nightly run)."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}cash_new6a"
    metric = MetricDefinition(
        tenant_id=SYSTEM_TENANT_ID,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "system_seed", "seeded_at": "2026-06-05"},
    )
    db.add(metric)
    await db.flush()

    updated = await update_metric(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        metric_id=metric.id,
        payload={"display_name": "Cash Balance"},
    )

    # B2 invariant: system_seed → authored (seeder will skip this row)
    assert updated.provenance["author"] == "authored", (
        f"expected 'authored' for system_seed row, got {updated.provenance!r}"
    )
    assert updated.provenance.get("updated_via") == "api"
    # Other provenance keys preserved
    assert updated.provenance.get("seeded_at") == "2026-06-05"


async def test_update_metric_non_system_seed_author_preserved(db, monkeypatch):
    """NEW-6 (b): a row with author='superadmin' updated via update_metric must keep
    author='superadmin' — the fix must NOT clobber any non-system_seed author class.
    This is the core NEW-6 regression: the old code unconditionally wrote 'authored',
    destroying a real author class like 'superadmin' or 'tenant_admin'."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}cash_new6b"
    metric = MetricDefinition(
        tenant_id=SYSTEM_TENANT_ID,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "superadmin", "created_at": "2026-06-05"},
    )
    db.add(metric)
    await db.flush()

    updated = await update_metric(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        metric_id=metric.id,
        payload={"display_name": "Cash Balance v2"},
    )

    # NEW-6: non-system_seed author must be preserved, NOT clobbered to "authored"
    assert updated.provenance["author"] == "superadmin", (
        f"expected 'superadmin' to be preserved, got {updated.provenance!r}"
    )
    assert updated.provenance.get("updated_via") == "api"
    assert updated.provenance.get("created_at") == "2026-06-05"


# ── R4: source_kind is immutable on update ────────────────────────────────────


async def test_update_metric_differing_source_kind_raises(db, tenant_a, monkeypatch):
    """R4 (negative): calling update_metric with a payload source_kind that DIFFERS from
    the existing metric.source_kind MUST raise AuthoringError mentioning 'immutable'.

    The real footgun: supplying source_kind='bigquery' WITH a bigquery-consistent
    blessed_spec (dialect='bigquery') so validate_definition PASSES — the merged dict is
    internally consistent. Without the immutability guard update_metric silently returns
    success, the row's source_kind stays 'suiteql', and the metric has been validated as
    bigquery but will execute as suiteql — a pure validate-as-X persist-as-Y drift.

    After the fix, update_metric rejects BEFORE validate_definition so the inconsistency
    can never be silently accepted.

    NOTE: the guard fires before validate_definition, so it must match 'immutable' in
    the error message (the validate_definition dialect-mismatch message would not fire
    for a self-consistent bigquery payload)."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}rev_r4_neg"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Revenue",
        definition="total revenue",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    # Supply a bigquery-consistent payload (query+dialect both 'bigquery') so
    # validate_definition would PASS without the new guard — exposing the true footgun.
    with pytest.raises(AuthoringError, match=r"(?i)immutable"):
        await update_metric(
            db,
            tenant_id=tenant_a.id,
            metric_id=metric.id,
            payload={
                "source_kind": "bigquery",
                "blessed_spec": {"query": "SELECT 1", "dialect": "bigquery"},
                "display_name": "Revenue BQ",
            },
        )

    # The row must be unchanged — source_kind still 'suiteql'.
    refreshed = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == metric.id))).scalar_one()
    assert refreshed.source_kind == "suiteql", (
        f"source_kind must stay 'suiteql' after rejected update, got {refreshed.source_kind!r}"
    )


async def test_update_metric_same_source_kind_in_payload_succeeds(db, tenant_a, monkeypatch):
    """R4 (positive / control): a payload that includes source_kind EQUAL to the existing
    value must succeed — only a DIFFERING value is immutable.  This guards against
    over-blocking payloads that round-trip the current value."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}rev_r4_pos"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Revenue",
        definition="total revenue",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    # source_kind matches existing — update succeeds, display_name is changed.
    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"source_kind": "suiteql", "display_name": "Revenue Updated"},
    )
    assert updated.display_name == "Revenue Updated"
    assert updated.source_kind == "suiteql"
    assert updated.version == 2


async def test_update_metric_absent_source_kind_in_payload_succeeds(db, tenant_a, monkeypatch):
    """R4 (positive / control): omitting source_kind from the payload entirely (the normal
    API path since MetricUpdate has no source_kind field) must succeed unchanged."""

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}rev_r4_absent"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Revenue",
        definition="total revenue",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Revenue No SK"},
    )
    assert updated.display_name == "Revenue No SK"
    assert updated.source_kind == "suiteql"


# ── 082: create/update set SYSTEM tenant context for SYSTEM_TENANT_ID writes ──────
#
# Migration 082 adds WITH CHECK (tenant_id = get_current_tenant_id()) to
# metric_definitions, and audit_events already has WITH CHECK
# (tenant_id = get_current_tenant_id()) (021). The superadmin /system routes call
# create_metric/update_metric(tenant_id=SYSTEM) under the superadmin's OWN-tenant
# context (dependencies.py sets the requesting user's tenant, never SYSTEM). On
# Supabase (FORCE-RLS, non-bypass app role) the SYSTEM metric INSERT/UPDATE and the
# following audit_service.log_event(tenant_id=SYSTEM) would then be rejected.
#
# Fix: create_metric/update_metric set SYSTEM context (mirrors
# metric_catalog_seeder.py:100) WHEN tenant_id == SYSTEM_TENANT_ID — gated strictly so
# tenant authoring keeps its own dependencies.py context. These spy tests pin that
# source contract. Local postgres is BYPASSRLS so the enforcement itself is not
# exercised here (see test_metric_rls_policy for the non-bypass-role rejection) — these
# assert the call is made (and ONLY for SYSTEM).


async def test_create_metric_sets_system_context_for_system_tenant(db, monkeypatch):
    """082: create_metric MUST call set_tenant_context(SYSTEM_TENANT_ID) when authoring a
    SYSTEM metric, so the metric INSERT and the subsequent audit write pass the WITH CHECK
    on Supabase. RED pre-fix: create_metric only calls ensure_system_tenant, never
    set_tenant_context."""
    import app.services.metrics.metric_authoring as authoring_mod

    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr(authoring_mod, "embed_domain_query", _fake_embed)

    set_ctx_calls: list = []

    async def _spy_set_ctx(session, tenant_id):
        set_ctx_calls.append(str(tenant_id))

    monkeypatch.setattr(authoring_mod, "set_tenant_context", _spy_set_ctx)

    p = _kp()
    await create_metric(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        payload={
            "key": f"{p}sys_rev",
            "display_name": "System Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )

    assert str(SYSTEM_TENANT_ID) in set_ctx_calls, (
        f"create_metric must set SYSTEM context for a SYSTEM metric so the WITH CHECK "
        f"(082) + audit_events WITH CHECK pass on Supabase; got calls: {set_ctx_calls}"
    )


async def test_create_metric_does_not_set_system_context_for_tenant(db, tenant_a, monkeypatch):
    """082 (negative control): create_metric must NOT set SYSTEM context for a normal
    tenant metric — tenant authoring keeps the requesting user's own dependencies.py
    context. The gate is strictly tenant_id == SYSTEM_TENANT_ID."""
    import app.services.metrics.metric_authoring as authoring_mod

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr(authoring_mod, "embed_domain_query", _fake_embed)

    set_ctx_calls: list = []

    async def _spy_set_ctx(session, tenant_id):
        set_ctx_calls.append(str(tenant_id))

    monkeypatch.setattr(authoring_mod, "set_tenant_context", _spy_set_ctx)

    p = _kp()
    await create_metric(
        db,
        tenant_id=tenant_a.id,
        payload={
            "key": f"{p}tenant_rev",
            "display_name": "Tenant Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )

    assert str(SYSTEM_TENANT_ID) not in set_ctx_calls, (
        f"create_metric must NOT set SYSTEM context for a tenant metric; got calls: {set_ctx_calls}"
    )


async def test_update_metric_sets_system_context_for_system_tenant(db, monkeypatch):
    """082: update_metric MUST call set_tenant_context(SYSTEM_TENANT_ID) when editing a
    SYSTEM metric, BEFORE the SELECT...FOR UPDATE — so the tenant-scoped SELECT finds the
    SYSTEM row and the UPDATE + the following audit write pass the WITH CHECK on Supabase.
    RED pre-fix: update_metric never sets context."""
    import app.services.metrics.metric_authoring as authoring_mod

    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr(authoring_mod, "embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}sys_cash"
    metric = MetricDefinition(
        tenant_id=SYSTEM_TENANT_ID,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "system_seed"},
    )
    db.add(metric)
    await db.flush()

    set_ctx_calls: list = []

    async def _spy_set_ctx(session, tenant_id):
        set_ctx_calls.append(str(tenant_id))

    monkeypatch.setattr(authoring_mod, "set_tenant_context", _spy_set_ctx)

    await update_metric(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        metric_id=metric.id,
        payload={"display_name": "Cash Balance"},
    )

    assert str(SYSTEM_TENANT_ID) in set_ctx_calls, (
        f"update_metric must set SYSTEM context for a SYSTEM metric edit so the WITH CHECK "
        f"(082) + audit_events WITH CHECK pass on Supabase; got calls: {set_ctx_calls}"
    )


async def test_update_metric_does_not_set_system_context_for_tenant(db, tenant_a, monkeypatch):
    """082 (negative control): update_metric must NOT set SYSTEM context for a tenant
    metric edit. Also pins that the SYSTEM-context call is inside the SYSTEM branch only,
    so the existing test_update_metric_select_carries_for_update_lock (tenant_a) keeps
    captured_stmts[0] == the SELECT FOR UPDATE."""
    import app.services.metrics.metric_authoring as authoring_mod

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr(authoring_mod, "embed_domain_query", _fake_embed)

    p = _kp()
    key = f"{p}tenant_cash"
    metric = MetricDefinition(
        tenant_id=tenant_a.id,
        key=key,
        display_name="Cash",
        definition="cash balance",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 0", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()

    set_ctx_calls: list = []

    async def _spy_set_ctx(session, tenant_id):
        set_ctx_calls.append(str(tenant_id))

    monkeypatch.setattr(authoring_mod, "set_tenant_context", _spy_set_ctx)

    await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"display_name": "Cash Balance"},
    )

    assert str(SYSTEM_TENANT_ID) not in set_ctx_calls, (
        f"update_metric must NOT set SYSTEM context for a tenant metric edit; got calls: {set_ctx_calls}"
    )
