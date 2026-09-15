"""Existing-company conversion must preserve data and reject the wrong target."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.core.dependencies import has_permission
from app.core.encryption import encrypt_credentials
from app.models.chat import ChatMessage, ChatSession
from app.models.connection import Connection
from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.tenant import TenantConfig
from app.services.audit_service import log_event
from app.services.company_bootstrap import adopt_existing_company, validate_company_database
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_tenant,
    create_test_user,
    make_auth_headers,
)


@pytest.fixture
def company_mode(monkeypatch):
    monkeypatch.setattr(settings, "SINGLE_COMPANY", True)


async def snapshot(db):
    """Compare every persisted row, including fields unrelated to this change."""
    tables = (
        (await db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")))
        .scalars()
        .all()
    )
    return {
        table: (await db.execute(text(f'SELECT to_jsonb(t)::text FROM "{table}" t ORDER BY to_jsonb(t)::text')))
        .scalars()
        .all()
        for table in tables
    }


async def arguments(db, tenant):
    return dict(
        expected_tenant_id=tenant.id,
        expected_slug=tenant.slug,
        expected_database=(await db.execute(text("SELECT current_database()"))).scalar_one(),
        expected_system_identifier=(
            await db.execute(text("SELECT system_identifier::text FROM pg_control_system()"))
        ).scalar_one(),
    )


async def test_adoption_preview_apply_and_replay_preserve_all_other_rows(db, company_mode, monkeypatch):
    tenant = await create_test_tenant(db, slug="synthetic-existing-company")
    owner, _ = await create_test_user(db, tenant, role_name="admin")
    connection = Connection(
        tenant_id=tenant.id,
        provider="netsuite",
        label="Synthetic",
        encrypted_credentials=encrypt_credentials({"token": "synthetic-only"}),
        created_by=owner.id,
    )
    db.add(connection)
    await db.flush()
    job = Job(tenant_id=tenant.id, job_type="scheduled_job", status="failed", connection_id=connection.id)
    conversation = ChatSession(tenant_id=tenant.id, user_id=owner.id, title="Existing discussion")
    db.add_all([job, conversation])
    await db.flush()
    db.add_all(
        [
            Schedule(
                tenant_id=tenant.id,
                owner_id=owner.id,
                name="Paused workflow",
                schedule_type="job",
                instruction="Preserve these company instructions",
                is_active=False,
                plan_status="pending_approval",
                plan_version=3,
                budget_json={"usd": 1},
                retry_job_id=job.id,
            ),
            ChatMessage(tenant_id=tenant.id, session_id=conversation.id, role="user", content="Preserved context"),
        ]
    )
    config = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id))).scalar_one()
    config.account_mappings = {"synthetic_account": "original_mapping"}
    config.ai_api_key_encrypted = encrypt_credentials({"key": "synthetic-provider-key"})
    run = await create_test_recon_run(db, tenant.id)
    await create_test_recon_result(db, tenant.id, run.id, evidence={"source": "synthetic-preserved-evidence"})
    await log_event(db, tenant.id, category="test", action="preserved.audit", payload={"original": True})
    await db.commit()
    soul = AsyncMock()
    monkeypatch.setattr("app.services.soul_service.seed_default_soul", soul)
    args = await arguments(db, tenant)
    before = await snapshot(db)
    permissions = [await has_permission(db, owner.id, p) for p in ("tenant.manage", "connections.manage")]
    preview = await adopt_existing_company(db, **args)
    assert preview[1] is False
    assert await snapshot(db) == before
    converted, changed = await adopt_existing_company(db, **args, apply=True)
    assert changed and converted.id == tenant.id
    assert converted.plan == "self_hosted" and converted.plan_expires_at is None
    after = await snapshot(db)
    for table in before.keys() - {"tenants", "audit_events"}:
        assert after[table] == before[table], table
    for original, current in zip(before["tenants"], after["tenants"], strict=True):
        old, new = json.loads(original), json.loads(current)
        if old["id"] == str(tenant.id):
            for key in old.keys() - {"plan", "plan_expires_at", "updated_at"}:
                assert old[key] == new[key], key
        else:
            assert old == new
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert set(before["audit_events"]) <= set(after["audit_events"])
    event = json.loads(next(iter(set(after["audit_events"]) - set(before["audit_events"]))))
    assert event["tenant_id"] == str(tenant.id)
    assert event["action"] == "company.adopt" and event["actor_type"] == "system"
    assert event["payload"]["before"]["plan"] == "free"
    assert event["payload"]["after"] == {"plan": "self_hosted", "plan_expires_at": None}
    assert event["payload"]["entitlement_changes"]["chat_api"] == {"before": False, "after": True}
    assert event["payload"]["destination_system_identifier"] == args["expected_system_identifier"]
    assert [await has_permission(db, owner.id, p) for p in ("tenant.manage", "connections.manage")] == permissions
    assert (await validate_company_database(db)).id == tenant.id
    _, changed = await adopt_existing_company(db, **args, apply=True)
    assert changed is False
    assert await snapshot(db) == after
    soul.assert_not_awaited()


@pytest.mark.parametrize(
    "mismatch", ["database", "cluster", "id", "slug", "shared", "orphan", "inactive", "no_admin", "mode"]
)
async def test_adoption_rejects_wrong_database_or_company_without_any_changes(db, company_mode, monkeypatch, mismatch):
    tenant = await create_test_tenant(db, slug="expected-company")
    if mismatch != "no_admin":
        await create_test_user(db, tenant, role_name="admin")
    args = await arguments(db, tenant)
    if mismatch == "database":
        args["expected_database"] = "wrong-database"
    elif mismatch == "cluster":
        args["expected_system_identifier"] = "0"
    elif mismatch == "id":
        args["expected_tenant_id"] = uuid.uuid4()
    elif mismatch == "slug":
        args["expected_slug"] = "wrong-company"
    elif mismatch == "shared":
        await create_test_tenant(db, slug="unrelated-company")
    elif mismatch == "orphan":
        db.add(Job(tenant_id=uuid.uuid4(), job_type="unrelated-orphan", status="completed"))
    elif mismatch == "inactive":
        tenant.is_active = False
    elif mismatch == "mode":
        monkeypatch.setattr(settings, "SINGLE_COMPANY", False)
    await db.commit()
    before = await snapshot(db)
    with pytest.raises(ValueError):
        await adopt_existing_company(db, **args, apply=True)
    assert await snapshot(db) == before


async def test_adopted_company_admin_and_invited_member_keep_their_permissions(client, db, company_mode, monkeypatch):
    from app.models.user import User
    from app.services.invite_service import accept_invite, create_invite

    tenant = await create_test_tenant(db, slug="invite-company")
    admin, _ = await create_test_user(db, tenant, role_name="admin")
    original_password_hash = admin.hashed_password
    await db.commit()
    await adopt_existing_company(db, **await arguments(db, tenant), apply=True)
    admin_headers = make_auth_headers(admin)
    assert (await client.get("/api/v1/tenants/me", headers=admin_headers)).status_code == 200
    monkeypatch.setattr("app.services.invite_service.send_invite_email", AsyncMock())
    invite = await create_invite(db, tenant.id, "invited@example.com", "ops", admin.id, "Owner", "Example")
    await db.commit()
    member, _ = await accept_invite(db, invite.token, "Invited Member", password="Synthetic-Password1!")
    await db.commit()
    assert member.tenant_id == tenant.id
    assert await has_permission(db, member.id, "tenant.manage") is False
    assert (await client.get("/api/v1/tenants/me", headers=make_auth_headers(member))).status_code == 200
    assert (
        await db.execute(select(User.hashed_password).where(User.id == admin.id))
    ).scalar_one() == original_password_hash
    admin.global_role = "superadmin"
    await db.commit()
    assert (await client.get("/api/v1/admin/tenants", headers=admin_headers)).status_code == 404


async def test_failed_adoption_audit_rolls_back_company_change(db, company_mode, monkeypatch):
    tenant = await create_test_tenant(db, slug="atomic-company")
    await create_test_user(db, tenant, role_name="admin")
    await db.commit()
    args = await arguments(db, tenant)
    before = await snapshot(db)

    async def fail_after_update(*args, **kwargs):
        await db.flush()
        raise RuntimeError("audit failed")

    monkeypatch.setattr("app.services.company_bootstrap.log_event", fail_after_update)
    with pytest.raises(RuntimeError, match="audit failed"):
        await adopt_existing_company(db, **args, apply=True)
    assert await snapshot(db) == before
