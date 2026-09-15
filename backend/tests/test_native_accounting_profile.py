from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.services.transaction_ops import native_accounting_profile as mod
from app.services.transaction_ops import state_service as state
from tests.test_accounting_profiles import setup


def profile(account="6738075-sb1", subsidiary="5", prefix="second"):
    return {
        "schema_version": 1,
        "enabled": True,
        "account_id": account,
        "subsidiary_id": subsidiary,
        "role_id": "7",
        "accounting_book_id": "1",
        "tax_regime": "legacy",
        "treatment": "restore_existing_source_tax_allocation",
        "source_adapter": "solidus",
        "fields": {
            "order_reference": f"custbody_{prefix}_order",
            "source_line_id": f"custcol_{prefix}_line",
            "original_sku": f"custcol_{prefix}_sku",
            "vat_amount": f"custcol_{prefix}_vat",
        },
        "ar_account_ids": ["11"],
        "tax_account_ids": ["13"],
        "adjustment_account_ids": ["12"],
    }


async def test_configuration_is_current_tenant_scoped_audited_and_never_approval(db, admin_user):
    actor = admin_user[0]
    config, connection, _ = await setup(db, actor, legacy=True)
    snapshot = deepcopy(config.mapping_json)
    assert await mod.get_profile(db, actor.tenant_id, config) is None
    result = await mod.configure_profile(db, actor.tenant_id, config.id, profile(), actor=actor)
    assert (await mod.get_profile(db, actor.tenant_id, config))["fields"] == profile()["fields"]
    audit = await db.scalar(select(AuditEvent).where(AuditEvent.id == result["audit_id"]))
    assert audit.actor_id == actor.id and audit.payload["financial_writes"] == 0
    assert audit.payload["financial_approval"] is None
    assert connection.metadata_json["unrelated"] == "preserved" and config.mapping_json == snapshot
    await mod.configure_profile(db, actor.tenant_id, config.id, None, actor=actor)
    assert await mod.get_profile(db, actor.tenant_id, config) is None


@pytest.mark.parametrize(
    "change", ["account", "subsidiary", "foreign_actor", "foreign_config", "connection", "disabled", "invalid_field"]
)
async def test_scope_validation_prevents_cross_tenant_or_custom_field_escalation(db, admin_user, admin_user_b, change):
    actor, foreign = admin_user[0], admin_user_b[0]
    config, connection, _ = await setup(db, actor)
    value = profile()
    if change == "account":
        value["account_id"] = "999"
    if change == "subsidiary":
        value["subsidiary_id"] = "99"
    if change == "invalid_field":
        value["fields"]["vat_amount"] = "quantity"
    if change == "disabled":
        config.enabled = False
    if change == "connection":
        connection.metadata_json = {}
    await db.flush()
    tenant = foreign.tenant_id if change == "foreign_config" else actor.tenant_id
    who = foreign if change in {"foreign_config", "foreign_actor"} else actor
    with pytest.raises(state.StateError):
        await mod.configure_profile(db, tenant, config.id, value, actor=who)


async def test_revision_tampering_and_fresh_revocation_are_detected(db, admin_user):
    actor = admin_user[0]
    config, connection, _ = await setup(db, actor)
    await mod.configure_profile(db, actor.tenant_id, config.id, profile(), actor=actor)
    original = deepcopy(connection.metadata_json)
    changed = deepcopy(original)
    next(iter(changed[mod.NAMESPACE].values()))["profile"]["role_id"] = "9"
    connection.metadata_json = changed
    await db.flush()
    with pytest.raises(ValueError, match="revision_mismatch"):
        await mod.get_profile(db, actor.tenant_id, config)
    connection.metadata_json = original
    await db.flush()
    async with AsyncSession(
        bind=await db.connection(), expire_on_commit=False, join_transaction_mode="create_savepoint"
    ) as other:
        from app.models.connection import Connection

        current = await other.get(Connection, connection.id)
        current.status = "revoked"
        await other.commit()
    assert connection.status == "active"
    assert await mod.get_profile(db, actor.tenant_id, config) is None
