"""Dedicated installation regressions against an isolated migrated Postgres DB."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.security import verify_password
from app.models.audit import AuditEvent
from app.models.feature_flag import TenantFeatureFlag
from app.models.tenant import Tenant
from app.models.user import Role, User, UserRole
from app.schemas.auth import RegisterRequest
from app.services.company_bootstrap import bootstrap_company, validate_company_database
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


@pytest.fixture
def company_mode(monkeypatch):
    monkeypatch.setattr(settings, "SINGLE_COMPANY", True)


@pytest.fixture
def registration():
    return RegisterRequest(
        tenant_name="Example Company",
        tenant_slug="example-company",
        email="owner@example.com",
        full_name="Company Owner",
        password="Local-Test-Password1!",
    )


async def test_bootstrap_creates_admin_flags_and_audit_atomically(db, company_mode, registration, monkeypatch):
    soul = AsyncMock()
    monkeypatch.setattr("app.services.soul_service.seed_default_soul", soul)
    tenant, created = await bootstrap_company(db, registration)
    assert created and tenant.plan == "self_hosted" and tenant.plan_expires_at is None
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    assert user.global_role == "user"
    assert verify_password(registration.password, user.hashed_password)
    role = (await db.execute(select(Role.name).join(UserRole).where(UserRole.user_id == user.id))).scalar_one()
    assert role == "admin"
    flags = dict(
        (
            await db.execute(
                select(TenantFeatureFlag.flag_key, TenantFeatureFlag.enabled).where(
                    TenantFeatureFlag.tenant_id == tenant.id
                )
            )
        ).all()
    )
    assert all(flags[key] for key in ("workspace", "chat", "mcp_tools", "byok_ai", "reconciliation"))
    assert not any(flags[key] for key in ("autonomous_recon", "recon_scheduled_runs", "recon_resolution_agent"))
    assert (
        await db.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "company.bootstrap"))
    ).scalar_one() == 1
    soul.assert_not_awaited()
    assert (await validate_company_database(db)).id == tenant.id


async def test_repeat_bootstrap_never_resets_password_or_flags(db, company_mode, registration):
    tenant, _ = await bootstrap_company(db, registration)
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    original_hash = user.hashed_password
    flag = (
        await db.execute(
            select(TenantFeatureFlag).where(
                TenantFeatureFlag.tenant_id == tenant.id, TenantFeatureFlag.flag_key == "byok_ai"
            )
        )
    ).scalar_one()
    flag.enabled = False
    await db.commit()
    again, created = await bootstrap_company(db, registration.model_copy(update={"password": "Different1!"}))
    assert again.id == tenant.id and not created
    await db.refresh(user)
    await db.refresh(flag)
    assert user.hashed_password == original_hash
    assert flag.enabled is False
    with pytest.raises(ValueError, match="different company"):
        await bootstrap_company(db, registration.model_copy(update={"tenant_slug": "another-company"}))


async def test_bootstrap_refuses_existing_hosted_company(db, company_mode, registration):
    await create_test_tenant(db, slug="already-hosted")
    with pytest.raises(ValueError, match="not been bootstrapped"):
        await bootstrap_company(db, registration)


async def test_single_company_validation_rejects_multiple_companies(db, company_mode):
    await create_test_tenant(db, slug="one", plan="self_hosted")
    await create_test_tenant(db, slug="two", plan="self_hosted")
    with pytest.raises(ValueError, match="exactly one"):
        await validate_company_database(db)


async def test_bootstrap_requires_explicit_mode(db, registration, monkeypatch):
    monkeypatch.setattr(settings, "SINGLE_COMPANY", False)
    with pytest.raises(ValueError, match="SINGLE_COMPANY"):
        await bootstrap_company(db, registration)


async def test_missing_admin_role_cannot_create_half_configured_company(db, company_mode, registration):
    role = (await db.execute(select(Role).where(Role.name == "admin"))).scalar_one()
    role.name = "temporarily-unavailable"
    await db.flush()
    with pytest.raises(ValueError, match="Admin role is missing"):
        await bootstrap_company(db, registration)
    assert (
        await db.execute(select(func.count()).select_from(Tenant).where(Tenant.id != uuid.UUID(int=0)))
    ).scalar_one() == 0


async def test_public_registration_is_closed_before_bootstrap(client, company_mode, registration):
    response = await client.post("/api/v1/auth/register", json=registration.model_dump(mode="json"))
    assert response.status_code == 404


async def test_self_hosted_login_has_no_trial_expiry_and_can_exceed_five_schedules(
    client, db, company_mode, registration
):
    tenant, _ = await bootstrap_company(db, registration)
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    headers = make_auth_headers(user)
    # The company continues working long after the former free-trial window.
    tenant.created_at = datetime.now(timezone.utc) - timedelta(days=90)
    await db.commit()
    response = await client.get("/api/v1/tenants/me/plan", headers=headers)
    assert response.status_code == 200
    assert response.json()["plan"] == "self_hosted"
    assert response.json()["plan_expires_at"] is None
    assert response.json()["limits"]["max_schedules"] == -1
    for i in range(6):
        response = await client.post(
            "/api/v1/schedules",
            headers=headers,
            json={"name": f"Workflow {i}", "schedule_type": "report", "cron_expression": "0 9 * * 1"},
        )
        assert response.status_code == 201, response.text
    user.global_role = "superadmin"
    await db.commit()
    assert (await client.get("/api/v1/admin/tenants", headers=headers)).status_code == 404
    tenant.is_active = False
    await db.commit()
    assert (await client.get("/api/v1/tenants/me", headers=headers)).status_code == 403


async def test_self_hosted_permissions_are_still_required(client, db, company_mode):
    tenant = await create_test_tenant(db, plan="self_hosted")
    tenant.plan_expires_at = None
    user, _ = await create_test_user(db, tenant, role_name="readonly")
    await db.commit()
    response = await client.post(
        "/api/v1/schedules",
        headers=make_auth_headers(user),
        json={"name": "Unauthorized", "schedule_type": "report"},
    )
    assert response.status_code == 403


async def test_company_workflow_still_requires_plan_approval(client, db, company_mode, registration, monkeypatch):
    from app.services.jobs.compiler import CompiledPlan

    tenant, _ = await bootstrap_company(db, registration)
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    headers = make_auth_headers(user)
    compiler = AsyncMock(
        return_value=CompiledPlan(
            plan_json={"steps": [{"id": "query", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]},
            summary_line="Read the connected warehouse",
            kinds={"read"},
            model="test-only-no-provider-call",
        )
    )
    monkeypatch.setattr("app.services.schedule_service.compile_instruction", compiler)
    from unittest.mock import Mock

    dispatch = Mock()
    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", dispatch)
    response = await client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "Company weekly workflow",
            "instruction": "Read our warehouse every Monday",
            "cron_expression": "0 9 * * 1",
            "timezone": "America/Los_Angeles",
        },
    )
    assert response.status_code == 201, response.text
    schedule_id = response.json()["id"]
    assert response.json()["plan_status"] == "pending_approval"
    assert (await client.post(f"/api/v1/schedules/{schedule_id}/run", headers=headers, json={})).status_code == 409
    dispatch.assert_not_called()
    response = await client.post(f"/api/v1/schedules/{schedule_id}/approve", headers=headers)
    assert response.status_code == 200, response.text
    response = await client.post(f"/api/v1/schedules/{schedule_id}/run", headers=headers, json={})
    assert response.status_code == 202, response.text
    dispatch.assert_called_once()


async def test_concurrent_installers_create_only_one_company(company_mode, registration):
    """Use real commits on two connections; a savepoint fixture cannot prove this."""
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.tenant import TenantConfig

    engine = create_async_engine(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    slug = f"bootstrap-race-{uuid.uuid4().hex}"
    request = registration.model_copy(update={"tenant_slug": slug})

    async def install():
        async with sessions() as session:
            tenant, created = await bootstrap_company(session, request)
            return tenant.id, created

    try:
        first, second = await asyncio.gather(install(), install())
        assert first[0] == second[0]
        assert sorted([first[1], second[1]]) == [False, True]
        async with sessions() as session:
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.tenant_id == first[0], AuditEvent.action == "company.bootstrap")
                )
            ).scalar_one() == 1
    finally:
        async with sessions() as session:
            tenant_id = (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one_or_none()
            if tenant_id is not None:
                # Exact synthetic tenant only; no broad database reset.
                for model in (AuditEvent, TenantFeatureFlag, UserRole, TenantConfig, User):
                    await session.execute(delete(model).where(model.tenant_id == tenant_id))
                await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
                await session.commit()
        await engine.dispose()
