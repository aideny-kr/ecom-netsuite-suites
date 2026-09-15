import uuid

import pytest
from sqlalchemy import select, update

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.feature_flag import TenantFeatureFlag
from app.models.transaction_ops import TransactionConfig
from app.schemas.transaction_runs import ConfigControl
from app.services.transaction_ops import framework_defaults as defaults
from app.services.transaction_ops import state_service


async def connections(db, tenant_id, account="6738075"):
    source = Connection(
        tenant_id=tenant_id,
        provider="solidus",
        label="Solidus",
        status="active",
        metadata_json={"api_profile": "framework_sync"},
        encrypted_credentials=encrypt_credentials(
            {
                "api_profile": "framework_sync",
                "base_url": "https://private-direct-access.frame.work/api/",
                "token": "private-test-key",
                "auth_type": "api_key",
                "header_name": "X-Store-Token",
            }
        ),
    )
    target = Connection(
        tenant_id=tenant_id,
        provider="netsuite",
        label="NetSuite",
        status="active",
        encrypted_credentials=encrypt_credentials({"account_id": account, "access_token": "private"}),
    )
    db.add_all([source, target])
    await db.flush()
    return source, target


async def test_backend_defaults_create_verified_entity_scopes_without_setup_fields(db, admin_user):
    user, _ = admin_user
    source, target = await connections(db, user.tenant_id)
    rows = await defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user)
    assert {row.subsidiary_id for row in rows} == {"1", "2", "4", "5"}
    for row in rows:
        assert row.source_connection_id == source.id and row.netsuite_connection_id == target.id
        assert row.mapping_json["reference_field"] == "tranid"
        assert row.mapping_json["action_mode"] == "propose_actions"
        assert row.schedule_enabled and row.interval_minutes == 1440
        assert row.mapping_json["business_entity_subsidiaries"] == {
            next(
                name for name, subsidiary in defaults.ENTITY_SUBSIDIARIES.items() if subsidiary == row.subsidiary_id
            ): row.subsidiary_id,
        }


async def test_defaults_are_idempotent_and_preserve_operator_pause(db, admin_user):
    user, _ = admin_user
    source, _ = await connections(db, user.tenant_id)
    first = await defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user)
    await state_service.control_config(
        db, user.tenant_id, first[0].id, ConfigControl(enabled=True, schedule_enabled=False), actor=user
    )
    second = await defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user)
    assert {row.id for row in second} == {row.id for row in first}
    assert next(row for row in second if row.id == first[0].id).schedule_enabled is False
    assert (
        len((await db.scalars(select(TransactionConfig).where(TransactionConfig.tenant_id == user.tenant_id))).all())
        == 4
    )


async def test_other_account_does_not_inherit_framework_subsidiary_mappings(db, admin_user):
    user, _ = admin_user
    source, _ = await connections(db, user.tenant_id, account="9999999")
    with pytest.raises(state_service.StateError, match="framework_destination_unavailable"):
        await defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user)


async def test_bootstrap_cannot_use_another_tenants_source(db, admin_user, admin_user_b):
    source, _ = await connections(db, admin_user_b[0].tenant_id)
    with pytest.raises(state_service.StateError, match="source_unavailable"):
        await defaults.ensure_framework_configs(db, admin_user[0].tenant_id, source.id, actor=admin_user[0])


async def test_refund_defaults_bind_only_the_verified_tenant_owned_database_source(
    db, admin_user, admin_user_b, monkeypatch
):
    from app.models.celigo import CeligoFlowStep
    from tests.test_transaction_ops_state_db import seed_config

    user, _ = admin_user
    seed = await seed_config(db, user.tenant_id, user)
    step = await db.get(CeligoFlowStep, seed.source_step_id)
    step.adaptor_type = "RDBMSExport"
    monkeypatch.setattr(defaults, "FRAMEWORK_REFUND_STEP_ID", step.id)
    await db.flush()
    assert await defaults.refund_source_id(db, user.tenant_id) == str(step.id)
    assert await defaults.refund_source_id(db, admin_user_b[0].tenant_id) is None
    source, _ = await connections(db, user.tenant_id)
    configs = await defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user)
    assert all(row.mapping_json["solidus_refund_step_id"] == str(step.id) for row in configs)


async def test_bootstrap_rechecks_actor_permissions(db, readonly_user):
    user, _ = readonly_user
    source, _ = await connections(db, user.tenant_id)
    with pytest.raises(state_service.StateError, match="permission_denied"):
        await defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user)


async def test_bootstrap_api_requires_management_permission(client, db, readonly_user):
    await db.execute(
        update(TenantFeatureFlag)
        .where(
            TenantFeatureFlag.tenant_id == readonly_user[0].tenant_id,
            TenantFeatureFlag.flag_key.in_(("celigo", "reconciliation")),
        )
        .values(enabled=True)
    )
    response = await client.post(
        "/api/v1/transaction-ops/setup/defaults",
        headers=readonly_user[1],
        json={"source_connection_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403


async def test_bootstrap_api_returns_usable_scopes_without_credentials(client, db, admin_user):
    user, headers = admin_user
    await db.execute(
        update(TenantFeatureFlag)
        .where(
            TenantFeatureFlag.tenant_id == user.tenant_id,
            TenantFeatureFlag.flag_key.in_(("celigo", "reconciliation")),
        )
        .values(enabled=True)
    )
    source, _ = await connections(db, user.tenant_id)
    response = await client.post(
        "/api/v1/transaction-ops/setup/defaults", headers=headers, json={"source_connection_id": str(source.id)}
    )
    assert response.status_code == 200
    assert len(response.json()) == 4
    assert "private-test-key" not in response.text and "encrypted_credentials" not in response.text
