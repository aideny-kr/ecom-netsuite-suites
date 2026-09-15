from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.mcp_connector import McpConnector
from app.models.transaction_ops import TransactionConfig
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import replica_setup
from app.services.transaction_ops import state_service as state
from tests.test_metabase_replica_reader import BINDING
from tests.test_transaction_ops_state_db import seed_config


async def connector(db, actor):
    row = McpConnector(
        tenant_id=actor.tenant_id,
        provider="custom",
        label="Replica",
        server_url=BINDING["server_url"],
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="opaque",
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_verified_binding_versions_configs_atomically_and_retry_is_idempotent(db, admin_user, monkeypatch):
    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor, schedule_enabled=True)
    original = dict(old.mapping_json)
    c = await connector(db, actor)
    binding = {**BINDING, "connector_id": str(c.id)}
    verify = AsyncMock(return_value={"orders": [], "page_complete": True})
    monkeypatch.setattr(replica_setup.metabase_reader, "read_order_page", verify)
    rows = await replica_setup.bind_verified_replica(db, actor.tenant_id, [old.id], binding, actor=actor)
    new = rows[0]
    assert new.id != old.id and new.config_key != old.config_key
    assert new.enabled and new.schedule_enabled and not old.enabled and not old.schedule_enabled
    assert old.mapping_json == original
    assert new.mapping_json["metabase_replica"] == binding
    assert new.mapping_json["reconciliation_policy"]["daily_check_hour"] == 9
    assert c.metadata_json["transaction_replica"] == binding
    retry = await replica_setup.bind_verified_replica(db, actor.tenant_id, [old.id], binding, actor=actor)
    assert retry[0].id == new.id
    assert len(list((await db.scalars(select(TransactionConfig))).all())) == 2


@pytest.mark.asyncio
async def test_failed_verification_or_other_tenant_cannot_change_config(db, admin_user, tenant_b, monkeypatch):
    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor, schedule_enabled=True)
    c = await connector(db, actor)
    binding = {**BINDING, "connector_id": str(c.id)}
    with pytest.raises(state.StateError):
        await replica_setup.bind_verified_replica(db, tenant_b.id, [old.id], binding, actor=actor)
    monkeypatch.setattr(replica_setup.metabase_reader, "read_order_page", AsyncMock(side_effect=ValueError("failed")))
    with pytest.raises(state.StateError, match="replica_verification_failed"):
        await replica_setup.bind_verified_replica(db, actor.tenant_id, [old.id], binding, actor=actor)
    assert old.enabled and old.schedule_enabled and "transaction_replica" not in (c.metadata_json or {})


@pytest.mark.asyncio
async def test_cannot_replace_scope_while_a_run_is_pending(db, admin_user, monkeypatch):
    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor)
    await state.create_run(
        db, actor.tenant_id, old.id, RunCreate(evaluation_key="busy", order_references=["R123456789"]), actor=actor
    )
    c = await connector(db, actor)
    monkeypatch.setattr(replica_setup.metabase_reader, "read_order_page", AsyncMock())
    with pytest.raises(state.StateError, match="config_has_active_runs"):
        await replica_setup.bind_verified_replica(
            db, actor.tenant_id, [old.id], {**BINDING, "connector_id": str(c.id)}, actor=actor
        )
    assert old.enabled


@pytest.mark.asyncio
async def test_current_configs_hide_predecessors_but_keep_paused_revision(db, admin_user, monkeypatch):
    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor)
    old.enabled = False
    await db.flush()
    c = await connector(db, actor)
    monkeypatch.setattr(replica_setup.metabase_reader, "read_order_page", AsyncMock(return_value={"orders": []}))
    new = (
        await replica_setup.bind_verified_replica(
            db, actor.tenant_id, [old.id], {**BINDING, "connector_id": str(c.id)}, actor=actor
        )
    )[0]
    assert not new.enabled and not new.schedule_enabled
    current = await state.list_configs(db, actor.tenant_id)
    assert [r.id for r in current] == [new.id]
    assert (await state.get_config(db, actor.tenant_id, old.id)).id == old.id


@pytest.mark.asyncio
async def test_superseded_config_cannot_be_reactivated(db, admin_user, monkeypatch):
    from app.schemas.transaction_runs import ConfigControl

    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor)
    c = await connector(db, actor)
    monkeypatch.setattr(replica_setup.metabase_reader, "read_order_page", AsyncMock(return_value={"orders": []}))
    await replica_setup.bind_verified_replica(
        db, actor.tenant_id, [old.id], {**BINDING, "connector_id": str(c.id)}, actor=actor
    )
    with pytest.raises(state.StateError, match="config_superseded"):
        await state.control_config(
            db, actor.tenant_id, old.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
        )
