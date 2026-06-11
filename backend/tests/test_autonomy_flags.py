"""Bet 3 Rung 1 flags: scheduled recon runs + autonomy envelope are opt-in,
default OFF for every tenant (decision doc D5)."""

import uuid

from app.services import feature_flag_service


def test_rung1_flags_registered_and_default_off():
    assert feature_flag_service.DEFAULT_FLAGS["recon_scheduled_runs"] is False
    assert feature_flag_service.DEFAULT_FLAGS["autonomous_recon"] is False
    assert feature_flag_service.is_known_flag("recon_scheduled_runs")
    assert feature_flag_service.is_known_flag("autonomous_recon")


async def test_list_enabled_tenants_filters_by_flag_and_enabled(db, tenant_a, tenant_b):
    await feature_flag_service.set_flag(db, tenant_a.id, "recon_scheduled_runs", True)
    await feature_flag_service.set_flag(db, tenant_b.id, "recon_scheduled_runs", False)
    await feature_flag_service.set_flag(db, tenant_b.id, "autonomous_recon", True)

    enabled = await feature_flag_service.list_enabled_tenants(db, "recon_scheduled_runs")

    assert enabled == [tenant_a.id]
    assert all(isinstance(t, uuid.UUID) for t in enabled)


async def test_list_tenants_with_flags_requires_all_flags_and_active_tenant(db, tenant_a, tenant_b):
    """Fan-out gating helper: ALL flags enabled AND tenant active, one query."""
    await feature_flag_service.set_flag(db, tenant_a.id, "recon_scheduled_runs", True)
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    # tenant_b has both flags but is deactivated — must be excluded.
    await feature_flag_service.set_flag(db, tenant_b.id, "recon_scheduled_runs", True)
    await feature_flag_service.set_flag(db, tenant_b.id, "reconciliation", True)
    tenant_b.is_active = False
    await db.flush()

    out = await feature_flag_service.list_tenants_with_flags(db, ("recon_scheduled_runs", "reconciliation"))
    assert out == [tenant_a.id]

    # Missing one of the required flags → excluded.
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", False)
    out = await feature_flag_service.list_tenants_with_flags(db, ("recon_scheduled_runs", "reconciliation"))
    assert out == []
