"""
Test fixtures for the backend test suite.

Uses the Docker-composed PostgreSQL database.
Tests run against real Postgres to ensure RLS, UUID types, and JSON columns work correctly.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models.tenant import Tenant, TenantConfig
from app.models.user import Role, User, UserRole

# ---------------------------------------------------------------------------
# Per-test DB session — fresh engine + connection per test to avoid loop issues
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    """Provide a database session. Each test gets its own engine and a transaction that is rolled back."""
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
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
    plan: str = "trial",
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
async def finance_user(db: AsyncSession, tenant_a: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_a, role_name="finance")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def admin_user_b(db: AsyncSession, tenant_b: Tenant) -> tuple[User, dict]:
    user, _ = await create_test_user(db, tenant_b, role_name="admin")
    return user, make_auth_headers(user)
