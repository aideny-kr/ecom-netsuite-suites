"""Seed a throwaway tenant with a full Celigo flow-map fixture world, for
local/staging E2E runs against ``frontend/e2e/celigo-flow-pages.spec.ts``.

Usage:
    cd backend && .venv/bin/python -m scripts.seed_celigo_e2e --tenant-slug e2e-celigo

SAFETY GUARD (mirrors ``scripts/uat/recon_live_smoke.py``'s hard slug guard):
refuses any ``--tenant-slug`` other than ``uat-smoke`` or one starting with
``e2e-`` -- this script does a REAL commit against a REAL Postgres, never a
test-fixture rollback, so it must never be pointable at a real tenant by a
typo. ``--database-url`` defaults to a dedicated, disposable database
(``ecom_netsuite_flowpages``) -- deliberately NOT the shared local dev DB
(``ecom_netsuite``) other engineers' manual testing lives in, and NEVER
Supabase (there is no slug-based guard strong enough to make writing
directly to production data safe; this script only ever takes a
``postgresql+asyncpg://`` URL a human typed).

IDEMPOTENT: every insert below is preceded by a lookup on that row's own
natural key (tenant slug, connection provider, integration/flow celigo_id).
A second run against the same database finds every row already there and
changes nothing -- see ``_seed_fixture_world``'s early return.

WHAT IT SEEDS (Task 3's fixture world, the same shape
``backend/tests/api/test_celigo_flows_api.py::_seed_router_chain_flow``
builds for the API test suite, copied here rather than imported -- that
function is private to its own test module, and a test helper is not a
dependency this script should carry):
  - one tenant (created if the slug doesn't already resolve one) + one admin
    user (``E2E_EMAIL``/``E2E_PASSWORD`` env vars, or the printed defaults)
  - the ``celigo`` feature flag, ON
  - one ``connections`` row (provider ``celigo``, an ENCRYPTED placeholder
    credential -- never a real Celigo token)
  - one integration
  - the Multi-Subsidiary flow: source -> router 1 (a pass-through lookup
    carrying a shared ``preSavePage`` hook) -> router 2, two named branches
    ("Framework Intl" / "Framework Inc"), each: NetSuite lookup (search
    5090) -> add customer -> update customer -> add salesorder (carrying a
    diverged ``preMap`` hook, attached at both branches' final step)
  - one error signature with 10 OPEN ``celigo_flow_errors`` rows, all
    attributed to the "Lookup Customer" step on the Framework Intl branch
  - a paused flow (``disabled=True``) and an on-demand flow
    (``schedule=None``)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.encryption import encrypt_credentials, get_current_key_version
from app.core.security import hash_password
from app.models.celigo import (
    CeligoErrorSignature,
    CeligoFlow,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoIntegration,
    CeligoScript,
    CeligoScriptAttachment,
)
from app.models.tenant import Tenant, TenantConfig
from app.models.user import Role, User, UserRole

DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite_flowpages"
DEFAULT_EMAIL = "e2e-celigo@example.com"
DEFAULT_PASSWORD = "CeligoE2E-Passw0rd!"

# The one root cause every cross-surface consistency assertion pins on: 10
# open errors, 1 distinct signature, on the Framework Intl branch's customer
# lookup -- matches `cross-surface-counts.test.tsx` (Task 18) and the
# approved mockup's own "10 open · 1 root cause" / "Framework Intl" facts.
ERROR_COUNT = 10
# When the pretend sync last consulted each seeded flow's Celigo error summary
# (migration 098's `celigo_flows.errors_checked_at`) -- shortly after the chain
# flow's `last_executed_at`, so the page reads "checked N ago", never "errors
# not checked yet" (which is what a NULL honestly renders as).
ERRORS_CHECKED_AT = datetime(2026, 9, 2, 18, 12, tzinfo=timezone.utc)


def _guard_tenant_slug(slug: str) -> None:
    """Refuse any slug other than the two this script is allowed to touch --
    the recon live-smoke harness's own guard pattern (`scripts/uat/
    recon_live_smoke.py`), applied here before any write, not just before the
    destructive ones (this script has none, but a wrong tenant is still a
    wrong tenant)."""
    if slug != "uat-smoke" and not slug.startswith("e2e-"):
        raise SystemExit(
            f"SAFETY ABORT: refusing to seed tenant slug {slug!r} -- must be 'uat-smoke' or start with 'e2e-'"
        )


async def _get_or_create_tenant(db: AsyncSession, slug: str) -> Tenant:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant:
        return tenant
    tenant = Tenant(name=f"Celigo E2E ({slug})", slug=slug, plan="free", is_active=True)
    db.add(tenant)
    await db.flush()
    db.add(
        TenantConfig(
            tenant_id=tenant.id,
            posting_mode="lumpsum",
            posting_batch_size=100,
            posting_attach_evidence=False,
            # A tenant created by the normal register flow reaches this state
            # via the onboarding wizard; this script bypasses that flow
            # entirely, so it marks onboarding complete itself -- otherwise
            # the wizard overlay blocks every click on first login (found by
            # actually driving this tenant through Playwright, not inspected).
            onboarding_completed_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    return tenant


async def _get_or_create_user(db: AsyncSession, tenant: Tenant, email: str, password: str) -> User:
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id, User.email == email))).scalar_one_or_none()
    if user:
        return user
    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password(password),
        full_name="Celigo E2E",
        actor_type="user",
    )
    db.add(user)
    await db.flush()

    role = (await db.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role:
        db.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        await db.flush()
    else:
        # Never fatal -- a database seeded without the standard roles migration
        # still gets a usable tenant/connection/flow-map world; only the
        # E2E spec's LOGIN step would need the role, which the operator will
        # notice immediately (a 403, not a silent gap).
        print("  WARNING: no 'admin' role row found -- user created with no role assigned")
    return user


async def _enable_celigo_flag(db: AsyncSession, tenant_id) -> None:
    """Writes the same row the API's feature-flag service reads -- mirrors
    `tests/conftest.py::enable_feature_flag` (cache-bust, set, cache-bust),
    the real service function underneath it, not a test-only shortcut."""
    from app.services.feature_flag_service import clear_cache, set_flag

    clear_cache()
    await set_flag(db, tenant_id, "celigo", True)
    await db.flush()
    clear_cache()


async def _get_or_create_connection(db: AsyncSession, tenant_id) -> uuid.UUID:
    """`celigo_write_guard.py` refuses an ORM `db.add()` of a `provider='celigo'`
    `connections` row outside the connect/disconnect endpoints -- same reason
    `_make_connection` in `test_celigo_flows_api.py` uses raw SQL. The
    credential is Fernet-ENCRYPTED via the real `encrypt_credentials` (never a
    plaintext placeholder string) so a decrypt anywhere in the read path sees
    a well-formed (if inert) value, not a crash."""
    existing = (
        await db.execute(
            text("SELECT id FROM connections WHERE tenant_id = :tid AND provider = 'celigo'"),
            {"tid": str(tenant_id)},
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    conn_id = uuid.uuid4()
    # The environment this script runs in may have no real ENCRYPTION_KEY
    # configured (a fresh worktree checkout with no `.env`) -- same situation
    # `tests/conftest.py`'s session-scoped `_set_encryption_key` fixture
    # exists to paper over for the test suite. This connection's credential
    # is an inert placeholder that nothing ever decrypts for a real Celigo
    # call, so a freshly-generated key is exactly as good as the configured
    # one for this row's purposes -- it only has to encrypt/decrypt
    # SOMETHING well-formed, never a real secret.
    if not settings.ENCRYPTION_KEY or settings.ENCRYPTION_KEY == "change-me-generate-a-real-fernet-key":
        from cryptography.fernet import Fernet

        print("  NOTE: no real ENCRYPTION_KEY configured -- generating a throwaway one for this run")
        settings.ENCRYPTION_KEY = Fernet.generate_key().decode()
    encrypted = encrypt_credentials({"note": "e2e placeholder -- never a real Celigo token"})
    await db.execute(
        text(
            "INSERT INTO connections "
            "(id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant_id, 'celigo', 'Celigo (e2e)', 'active', :creds, :key_version)"
        ),
        {"id": conn_id, "tenant_id": str(tenant_id), "creds": encrypted, "key_version": get_current_key_version()},
    )
    await db.flush()
    return conn_id


INTEGRATION_CELIGO_ID = "e2e_integration"
CHAIN_FLOW_CELIGO_ID = "e2e_flow_chain"
PAUSED_FLOW_CELIGO_ID = "e2e_flow_paused"
ON_DEMAND_FLOW_CELIGO_ID = "e2e_flow_on_demand"


async def _seed_fixture_world(db: AsyncSession, tenant_id, conn_id: uuid.UUID) -> dict:
    """Task 3's fixture world -- copied (not imported) from
    `test_celigo_flows_api.py::_seed_router_chain_flow`'s ORM construction,
    plus the paused/on-demand flows and the 10-error signature this task's
    e2e spec needs. IDEMPOTENT: the integration's own celigo_id is the
    natural key checked here; a second run against the same database finds
    it and returns immediately without touching anything else."""
    existing = (
        await db.execute(
            select(CeligoIntegration).where(
                CeligoIntegration.tenant_id == tenant_id,
                CeligoIntegration.celigo_connection_id == conn_id,
                CeligoIntegration.celigo_id == INTEGRATION_CELIGO_ID,
            )
        )
    ).scalar_one_or_none()
    if existing:
        chain_flow = (
            await db.execute(
                select(CeligoFlow).where(
                    CeligoFlow.tenant_id == tenant_id,
                    CeligoFlow.integration_id == existing.id,
                    CeligoFlow.celigo_id == CHAIN_FLOW_CELIGO_ID,
                )
            )
        ).scalar_one()
        signature = (
            await db.execute(
                select(CeligoErrorSignature).where(
                    CeligoErrorSignature.tenant_id == tenant_id,
                    CeligoErrorSignature.celigo_connection_id == conn_id,
                    CeligoErrorSignature.fingerprint == "e2e_fp_lookup",
                )
            )
        ).scalar_one_or_none()
        return {
            "integration_id": existing.id,
            "chain_flow_id": chain_flow.id,
            "signature_id": signature.id if signature else None,
            "created": False,
        }

    integration = CeligoIntegration(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id=INTEGRATION_CELIGO_ID,
        name="Solidus + NetSuite",
        sandbox=False,
        mode="settings",
        description="Production integration (e2e fixture)",
        raw_json={},
    )
    db.add(integration)
    await db.flush()

    chain_flow = CeligoFlow(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        integration_id=integration.id,
        celigo_id=CHAIN_FLOW_CELIGO_ID,
        name="New Sales Order to NetSuite - Multi-Subsidiary",
        disabled=False,
        schedule="? 5,20,35,50 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23 ? * *",
        timezone="America/Los_Angeles",
        last_executed_at=datetime(2026, 9, 2, 17, 51, tzinfo=timezone.utc),
        celigo_last_modified=datetime(2026, 9, 2, tzinfo=timezone.utc),
        # Migration 098: the seed stands in for a sync that DID consult this
        # flow's error summary, so its zero/non-zero counts render as checked
        # ("checked N ago") rather than "errors not checked yet".
        errors_checked_at=ERRORS_CHECKED_AT,
        raw_json={
            "numOpenError": ERROR_COUNT,
            "lastErrorAt": None,
            "routers": [
                {
                    "id": "r1",
                    "name": "",
                    "branches": [
                        {"branchId": "b0", "name": "", "nextRouterId": "r2", "pageProcessors": [{}]},
                    ],
                },
                {
                    "id": "r2",
                    "name": "",
                    "routeRecordsTo": "first_matching_branch",
                    "routeRecordsUsing": "input_filters",
                    "branches": [
                        {"branchId": "bIntl", "name": "Framework Intl", "pageProcessors": [{}, {}, {}, {}]},
                        {"branchId": "bInc", "name": "Framework Inc", "pageProcessors": [{}, {}, {}, {}]},
                    ],
                },
            ],
        },
    )
    db.add(chain_flow)
    await db.flush()

    def step(
        celigo_id,
        role,
        adaptor,
        *,
        router=None,
        branch=None,
        seq=0,
        record_type=None,
        operation=None,
        search_id=None,
        reference_name=None,
    ):
        return CeligoFlowStep(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            flow_id=chain_flow.id,
            celigo_id=celigo_id,
            role=role,
            adaptor_type=adaptor,
            router_id=router,
            branch_id=branch,
            sequence=seq,
            record_type=record_type,
            operation=operation,
            search_id=search_id,
            reference_name=reference_name,
            raw_json={},
        )

    steps = [
        step("e2e_src", "generator", "HTTPExport", reference_name="Get New Sales Orders"),
        step(
            "e2e_lkp",
            "processor",
            "HTTPExport",
            router="r1",
            branch="b0",
            reference_name="Lookup Sales Orders (Multi-Subsidiary)",
        ),
    ]
    lookup_step_by_branch: dict[str, CeligoFlowStep] = {}
    for branch, suffix in (("bIntl", "BV"), ("bInc", "Inc")):
        lookup = step(
            f"e2e_cust_lkp_{branch}",
            "processor",
            "NetSuiteExport",
            router="r2",
            branch=branch,
            seq=0,
            record_type="customer",
            search_id="5090",
            reference_name="Lookup Customer",
        )
        lookup_step_by_branch[branch] = lookup
        steps += [
            lookup,
            step(
                f"e2e_cust_add_{branch}",
                "processor",
                "NetSuiteDistributedImport",
                router="r2",
                branch=branch,
                seq=1,
                record_type="customer",
                operation="add",
                reference_name=f"Import Customer ({suffix})",
            ),
            step(
                f"e2e_cust_upd_{branch}",
                "processor",
                "NetSuiteDistributedImport",
                router="r2",
                branch=branch,
                seq=2,
                record_type="customer",
                operation="update",
                reference_name="Update Currency",
            ),
            step(
                f"e2e_so_add_{branch}",
                "processor",
                "NetSuiteDistributedImport",
                router="r2",
                branch=branch,
                seq=3,
                record_type="salesorder",
                operation="add",
                reference_name=f"Add New Sales Order ({suffix})",
            ),
        ]
    db.add_all(steps)
    await db.flush()
    by_id = {s.celigo_id: s for s in steps}

    solo = CeligoScript(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id="e2e_scr_solo",
        name="sales_order_script_v2",
        content="function preSavePage(record) { return record; }",
        content_hash="e2e_hsolo",
        celigo_last_modified=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    fam = [
        CeligoScript(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            celigo_id=f"e2e_scr_fam{i}",
            source_id="e2e_scr_fam0" if i else None,
            name="ns_sales_order_premap",
            content=f"function preMap(options) {{ /* version {i} */ return options; }}",
            content_hash=f"e2e_hfam{i}",
            celigo_last_modified=datetime(2026, 1, 1 + i, tzinfo=timezone.utc),
        )
        for i in range(2)
    ]
    db.add_all([solo, *fam])
    await db.flush()

    atts = [
        CeligoScriptAttachment(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            flow_id=chain_flow.id,
            flow_step_id=by_id["e2e_lkp"].id,
            script_id=solo.id,
            script_celigo_id=solo.celigo_id,
            function_name="preSavePage",
            json_path="e2e_lkp.hooks.preSavePage",
            site_type="hook",
        )
    ]
    for branch in ("bIntl", "bInc"):
        atts.append(
            CeligoScriptAttachment(
                tenant_id=tenant_id,
                celigo_connection_id=conn_id,
                flow_id=chain_flow.id,
                flow_step_id=by_id[f"e2e_so_add_{branch}"].id,
                script_id=fam[1].id,
                script_celigo_id=fam[1].celigo_id,
                function_name="preMap",
                json_path=f"e2e_so_add_{branch}.hooks.preMap",
                site_type="hook",
            )
        )
    db.add_all(atts)
    await db.flush()

    # The one root cause -- 10 open errors, all on the Framework Intl branch's
    # customer lookup step (matches the e2e spec's own "opens the lookup
    # bubble's Errors tab" step and the mockup's "Framework Intl" facts).
    signature = CeligoErrorSignature(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        fingerprint="e2e_fp_lookup",
        source="import",
        code="ERR001",
        sample_message="Customer not found for search 5090",
        occurrence_count=ERROR_COUNT,
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    db.add(signature)
    await db.flush()

    lookup_step = lookup_step_by_branch["bIntl"]
    for i in range(ERROR_COUNT):
        db.add(
            CeligoFlowError(
                tenant_id=tenant_id,
                celigo_connection_id=conn_id,
                flow_id=chain_flow.id,
                flow_step_id=lookup_step.id,
                signature_id=signature.id,
                celigo_id=f"e2e_err_{i}",
                trace_key=f"e2e_trace_{i}",
                source="import",
                code="ERR001",
                message=f"Customer not found for search 5090 (occurrence {i + 1})",
                occurred_at=datetime.now(timezone.utc),
                retriable=True,
            )
        )
    await db.flush()

    # A paused flow ("kept: every 4 h" in the mockup's own vocabulary) and an
    # on-demand flow (no schedule) -- round out the three schedule buckets
    # the integrations/integration pages group by.
    db.add(
        CeligoFlow(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            integration_id=integration.id,
            celigo_id=PAUSED_FLOW_CELIGO_ID,
            name="Nightly Backfill (paused)",
            disabled=True,
            schedule="? 0 */4 * * *",
            errors_checked_at=ERRORS_CHECKED_AT,
            raw_json={},
        )
    )
    db.add(
        CeligoFlow(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            integration_id=integration.id,
            celigo_id=ON_DEMAND_FLOW_CELIGO_ID,
            name="Manual Resync",
            disabled=False,
            schedule=None,
            errors_checked_at=ERRORS_CHECKED_AT,
            raw_json={},
        )
    )
    await db.flush()

    return {
        "integration_id": integration.id,
        "chain_flow_id": chain_flow.id,
        "signature_id": signature.id,
        "created": True,
    }


async def seed(database_url: str, tenant_slug: str, email: str, password: str) -> dict:
    _guard_tenant_slug(tenant_slug)

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            tenant = await _get_or_create_tenant(db, tenant_slug)
            # Best-effort tenant scoping for the raw-SQL connection insert
            # below -- harmless (and a documented convention, see
            # `scripts/import_tenant.py`) whether or not RLS is enforced for
            # the role this URL connects as. `SET LOCAL` does not support bind
            # params, so this goes through the one sanctioned helper
            # (`set_tenant_context`), never an f-string built by hand.
            await set_tenant_context(db, str(tenant.id))

            await _enable_celigo_flag(db, tenant.id)
            conn_id = await _get_or_create_connection(db, tenant.id)
            world = await _seed_fixture_world(db, tenant.id, conn_id)
            user = await _get_or_create_user(db, tenant, email, password)

            await db.commit()

        result = {
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant_slug,
            "user_id": str(user.id),
            "user_email": email,
            "connection_id": str(conn_id),
            "integration_id": str(world["integration_id"]),
            "chain_flow_id": str(world["chain_flow_id"]),
            "signature_id": str(world["signature_id"]) if world["signature_id"] else None,
        }
        print(f"{'Already seeded' if not world['created'] else 'Seeded'} tenant {tenant_slug!r}:")
        for key, value in result.items():
            print(f"  {key}: {value}")
        if world["created"]:
            print(f"  (password for {email}: {password!r} -- not stored anywhere else, use E2E_PASSWORD)")
        return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant-slug", required=True, help="'uat-smoke' or an 'e2e-'-prefixed slug")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL, help=f"default: {DEFAULT_DATABASE_URL}")
    parser.add_argument("--email", default=os.environ.get("E2E_EMAIL", DEFAULT_EMAIL))
    parser.add_argument("--password", default=os.environ.get("E2E_PASSWORD", DEFAULT_PASSWORD))
    args = parser.parse_args()

    asyncio.run(seed(args.database_url, args.tenant_slug, args.email, args.password))


if __name__ == "__main__":
    main()
