"""
Test fixtures for the backend test suite.

Uses the Docker-composed PostgreSQL database.
Tests run against real Postgres to ensure RLS, UUID types, and JSON columns work correctly.
"""

import ssl
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models.feature_flag import TenantFeatureFlag
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.models.tenant import Tenant, TenantConfig
from app.models.user import Role, User, UserRole


def _is_supabase(url: str) -> bool:
    return "supabase.com" in url or "supabase.co" in url


# Use direct connection for tests (pooler doesn't support transactional rollback)
_test_db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
_test_connect_args: dict = {}
if _is_supabase(_test_db_url):
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    _test_connect_args["ssl"] = _ssl_ctx

# ---------------------------------------------------------------------------
# Generate a valid Fernet encryption key for tests (avoids placeholder rejection)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _set_encryption_key():
    """Ensure a valid Fernet key is available for encrypt/decrypt operations in tests."""
    settings.ENCRYPTION_KEY = Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# Per-test DB session — fresh engine + connection per test to avoid loop issues
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    """Provide a database session. Each test gets its own engine and a transaction that is rolled back."""
    engine = create_async_engine(_test_db_url, echo=False, connect_args=_test_connect_args)
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def app(db: AsyncSession):
    """Create a FastAPI app instance with the test DB session injected."""
    application = create_app()

    async def override_get_db():
        yield db

    application.dependency_overrides[get_db] = override_get_db
    return application


@pytest_asyncio.fixture
async def client(app) -> AsyncClient:
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tenant & User factories
# ---------------------------------------------------------------------------


async def create_test_tenant(
    db: AsyncSession,
    name: str = "Test Corp",
    slug: str | None = None,
    plan: str = "free",
) -> Tenant:
    """Create a test tenant with config."""
    slug = slug or f"test-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(
        name=name,
        slug=slug,
        plan=plan,
        plan_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        is_active=True,
    )
    db.add(tenant)
    await db.flush()

    config = TenantConfig(
        tenant_id=tenant.id,
        posting_mode="lumpsum",
        posting_batch_size=100,
        posting_attach_evidence=False,
    )
    db.add(config)
    await db.flush()

    # Seed default feature flags so require_feature("chat") etc. pass in tests
    from app.services.feature_flag_service import DEFAULT_FLAGS

    for flag_key, enabled in DEFAULT_FLAGS.items():
        db.add(TenantFeatureFlag(tenant_id=tenant.id, flag_key=flag_key, enabled=enabled))
    await db.flush()

    return tenant


async def create_test_user(
    db: AsyncSession,
    tenant: Tenant,
    email: str | None = None,
    password: str = "testpassword123",
    full_name: str = "Test User",
    role_name: str = "admin",
) -> tuple[User, str]:
    """Create a test user and return (user, raw_password). Also assigns the given role."""
    email = email or f"user-{uuid.uuid4().hex[:8]}@test.com"
    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        actor_type="user",
    )
    db.add(user)
    await db.flush()

    # Assign role
    result = await db.execute(select(Role).where(Role.name == role_name))
    role = result.scalar_one_or_none()
    if role:
        user_role = UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id)
        db.add(user_role)
        await db.flush()

    return user, password


def make_auth_headers(user: User) -> dict[str, str]:
    """Generate JWT auth headers for a test user."""
    token_data = {"sub": str(user.id), "tenant_id": str(user.tenant_id)}
    token = create_access_token(token_data)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Canonical parent-row factories — satisfy reconciliation_results FKs
# (reconciliation_results.payout_id -> payouts.id, .deposit_id -> netsuite_postings.id)
# ---------------------------------------------------------------------------


async def create_test_payout(
    db: AsyncSession,
    tenant_id,
    *,
    id=None,
    source_id: str = "po_test",
    amount: Decimal = Decimal("1000.00"),
    fee_amount: Decimal = Decimal("30.00"),
    net_amount: Decimal = Decimal("970.00"),
    currency: str = "USD",
    status: str = "paid",
    arrival_date: date | None = None,
) -> "Payout":  # noqa: F821
    """Seed a canonical ``payouts`` row so a result can reference it via payout_id.

    Pass ``id=`` (a UUID) to match the UUID a MatchCandidate.payout carries so the
    reconciliation_results_payout_id_fkey is satisfied. Flushes for its id.
    """
    from app.models.canonical import Payout

    payout = Payout(
        id=id or uuid.uuid4(),
        tenant_id=tenant_id,
        dedupe_key=f"payout-{uuid.uuid4().hex}",
        source="stripe",
        source_id=source_id,
        amount=amount,
        fee_amount=fee_amount,
        net_amount=net_amount,
        currency=currency,
        status=status,
        arrival_date=arrival_date or date(2026, 3, 10),
    )
    db.add(payout)
    await db.flush()
    return payout


async def create_test_netsuite_posting(
    db: AsyncSession,
    tenant_id,
    *,
    id=None,
    netsuite_internal_id: str = "12345",
    record_type: str = "custdep",
    transaction_date: date | None = None,
    amount: Decimal = Decimal("100.00"),
    currency: str = "USD",
    related_payout_id: str | None = None,
) -> "NetsuitePosting":  # noqa: F821
    """Seed a canonical ``netsuite_postings`` row so a result can reference it via deposit_id.

    Pass ``id=`` (a UUID) to match the UUID a deposit/NSPaymentRecord carries so the
    reconciliation_results_deposit_id_fkey is satisfied. Flushes for its id.
    """
    from app.models.canonical import NetsuitePosting

    posting = NetsuitePosting(
        id=id or uuid.uuid4(),
        tenant_id=tenant_id,
        dedupe_key=f"posting-{uuid.uuid4().hex}",
        source="netsuite",
        source_id=netsuite_internal_id or uuid.uuid4().hex,
        netsuite_internal_id=netsuite_internal_id,
        record_type=record_type,
        transaction_date=transaction_date or date(2026, 3, 16),
        amount=amount,
        currency=currency,
        related_payout_id=related_payout_id,
    )
    db.add(posting)
    await db.flush()
    return posting


# ---------------------------------------------------------------------------
# Reconciliation factories (run/result) — FK-ordered: run flushed before result
# ---------------------------------------------------------------------------


async def create_test_recon_run(
    db: AsyncSession,
    tenant_id,
    *,
    status: str = "completed",
    parameters: dict | None = None,
) -> ReconciliationRun:
    """Create a ReconciliationRun for tests. Flushes so its id is available for results."""
    run = ReconciliationRun(
        tenant_id=tenant_id,
        date_from=date(2026, 4, 20),
        date_to=date(2026, 4, 24),
        status=status,
        total_payouts=0,
        total_deposits=0,
        matched_count=0,
        exception_count=0,
        unmatched_count=0,
        total_variance=Decimal("0"),
        parameters=parameters or {"match_level": "order", "subsidiary_id": None},
    )
    db.add(run)
    await db.flush()
    return run


async def create_test_recon_result(
    db: AsyncSession,
    tenant_id,
    run_id,
    *,
    match_type: str = "deterministic",
    confidence: Decimal = Decimal("1.0"),
    status: str = "suggested",
    variance_type: str | None = None,
    variance_amount: Decimal = Decimal("0"),
    match_rule: str | None = "order_reference_exact",
    stripe_amount: Decimal = Decimal("10.00"),
    netsuite_amount: Decimal = Decimal("10.00"),
    currency: str = "USD",
    bucket: str | None = None,
) -> ReconciliationResult:
    """Create a ReconciliationResult bound to an existing run. Flushes for its id.

    R2a persists the four-bucket classification on the row (compute-at-write), and
    the read-side / SQL twin now select on that stored ``bucket`` column. So the
    factory mirrors production by computing ``bucket`` via ``classify()`` (R1
    parity — no materiality thresholds, so immaterial variance stays in
    auto_classifications/rules). Pass ``bucket=`` to override, e.g. to seed a
    *material* matched row stored as ``needs_review`` while keeping its
    ``match_type='deterministic'``.
    """
    from app.services.reconciliation.four_bucket_classifier import classify

    if bucket is None:
        bucket = classify(match_type, variance_type, variance_amount)
    result = ReconciliationResult(
        tenant_id=tenant_id,
        run_id=run_id,
        payout_id=None,
        deposit_id=None,
        match_type=match_type,
        confidence=confidence,
        status=status,
        bucket=bucket,
        stripe_amount=stripe_amount,
        netsuite_amount=netsuite_amount,
        variance_amount=variance_amount,
        variance_type=variance_type,
        currency=currency,
        match_rule=match_rule,
    )
    db.add(result)
    await db.flush()
    return result


# ---------------------------------------------------------------------------
# Convenience fixtures for common test scenarios
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant_a(db: AsyncSession) -> Tenant:
    return await create_test_tenant(db, name="Tenant A", slug=f"tenant-a-{uuid.uuid4().hex[:6]}")


@pytest_asyncio.fixture
async def tenant_b(db: AsyncSession) -> Tenant:
    return await create_test_tenant(db, name="Tenant B", slug=f"tenant-b-{uuid.uuid4().hex[:6]}")


@pytest_asyncio.fixture
async def admin_user(db: AsyncSession, tenant_a: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_a, role_name="admin")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def readonly_user(db: AsyncSession, tenant_a: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_a, role_name="readonly")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def member_user(db: AsyncSession, tenant_a: Tenant) -> tuple[User, dict]:
    # A non-admin user with no metrics.manage permission (role: readonly).
    user, _ = await create_test_user(db, tenant_a, role_name="readonly")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def finance_user(db: AsyncSession, tenant_a: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_a, role_name="finance")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def ops_user(db: AsyncSession, tenant_a: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_a, role_name="ops")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def admin_user_b(db: AsyncSession, tenant_b: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_b, role_name="admin")
    return user, make_auth_headers(user)
