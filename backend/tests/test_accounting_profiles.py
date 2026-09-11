"""Real database boundaries for current treatments and immutable review inputs."""

from copy import deepcopy

import pytest
from sqlalchemy import select, text

from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import accounting_profiles as profiles
from app.services.transaction_ops import state_service as state
from tests.test_sales_credit import inputs
from tests.test_transaction_ops_state_db import seed_config


async def setup(db, actor, *, legacy=False):
    profile = {**inputs()["review"]["sales_credit_profile"], "account_id": "6738075-sb1", "subsidiary_id": "5"}
    changes = {"mapping_json": {"reference_field": "tranid", "sales_credit_profile": profile}} if legacy else {}
    config = await seed_config(db, actor.tenant_id, actor, **changes)
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.metadata_json = {"account_id": "6738075_SB1", "unrelated": "preserved"}
    await db.flush()
    return config, connection, profile


async def test_enable_and_disable_during_active_review_preserve_snapshot_and_audit(db, admin_user):
    actor = admin_user[0]
    config, connection, profile = await setup(db, actor)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="profile-test", order_references=["R123456789"]),
        actor=actor,
    )
    before = deepcopy(run.config_snapshot)
    mapping = deepcopy(config.mapping_json)
    result = await profiles.configure_sales_credit_profile(db, actor.tenant_id, config.id, profile, actor=actor)
    await db.refresh(run)
    await db.refresh(config)
    assert run.status == "pending" and run.config_snapshot == before
    assert config.mapping_json == mapping and config.enabled
    assert await profiles.sales_credit_profile(db, actor.tenant_id, config) == profile
    assert connection.metadata_json["unrelated"] == "preserved"
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(connection.id), AuditEvent.action == "accounting.profile.configured"
        )
    )
    assert str(audit.id) == result["audit_id"] and audit.actor_id == actor.id
    assert audit.payload["financial_writes"] == 0 and audit.payload["financial_approval"] is None
    await profiles.configure_sales_credit_profile(db, actor.tenant_id, config.id, None, actor=actor)
    assert await profiles.sales_credit_profile(db, actor.tenant_id, config) is None


async def test_explicit_disable_overrides_legacy_and_fresh_read_ignores_orm_cache(db, admin_user):
    actor = admin_user[0]
    config, connection, profile = await setup(db, actor, legacy=True)
    assert await profiles.sales_credit_profile(db, actor.tenant_id, config) == profile
    await profiles.configure_sales_credit_profile(db, actor.tenant_id, config.id, None, actor=actor)
    assert await profiles.sales_credit_profile(db, actor.tenant_id, config) is None
    await profiles.configure_sales_credit_profile(db, actor.tenant_id, config.id, profile, actor=actor)
    await db.execute(
        text("UPDATE connections SET status='revoked' WHERE id=:id AND tenant_id=:tenant"),
        {"id": connection.id, "tenant": actor.tenant_id},
    )
    assert connection.status == "active"
    assert await profiles.sales_credit_profile(db, actor.tenant_id, config) is None


@pytest.mark.parametrize(
    "variant", ["account", "subsidiary", "foreign_actor", "foreign_config", "connection", "disabled", "permission"]
)
async def test_scope_and_permissions_fail_closed(db, admin_user, admin_user_b, variant):
    actor, foreign = admin_user[0], admin_user_b[0]
    config, connection, profile = await setup(db, actor)
    if variant == "account":
        profile["account_id"] = "999"
    elif variant == "subsidiary":
        profile["subsidiary_id"] = "9"
    elif variant == "connection":
        connection.metadata_json = {"account_id": "999"}
        await db.flush()
    elif variant == "disabled":
        config.enabled = False
        await db.flush()
    elif variant == "permission":
        await db.execute(
            text("DELETE FROM user_roles WHERE user_id=:actor AND tenant_id=:tenant"),
            {"actor": actor.id, "tenant": actor.tenant_id},
        )
    tenant = foreign.tenant_id if variant == "foreign_config" else actor.tenant_id
    who = foreign if variant in {"foreign_actor", "foreign_config"} else actor
    with pytest.raises(state.StateError):
        await profiles.configure_sales_credit_profile(db, tenant, config.id, profile, actor=who)


async def test_malformed_override_does_not_reenable_legacy_and_other_scope_isolated(db, admin_user):
    actor = admin_user[0]
    config, connection, profile = await setup(db, actor, legacy=True)
    scope = profiles.config_scope(config)
    key = state.business_digest(scope)
    connection.metadata_json = {
        **connection.metadata_json,
        profiles.NAMESPACE: {"other-scope": {"sales_credit_profile": None}},
    }
    await db.flush()
    assert await profiles.sales_credit_profile(db, actor.tenant_id, config) == profile
    connection.metadata_json = {**connection.metadata_json, profiles.NAMESPACE: {key: {"scope": scope}}}
    await db.flush()
    with pytest.raises(ValueError):
        await profiles.sales_credit_profile(db, actor.tenant_id, config)
