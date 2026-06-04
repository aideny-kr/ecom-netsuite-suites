"""PART ④: shared load_materiality helper (dedup).

The identical ``_load_materiality`` bodies on ReconJobRunner and OrderReconJob now
delegate to one shared async helper in
``app.services.reconciliation.materiality``. Behavior must be preserved exactly:
returns the tenant's (recon_materiality_abs, recon_materiality_pct), falling back
to the $50 / 1% defaults when no TenantConfig row exists.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.tenant import TenantConfig
from app.services.reconciliation.materiality import (
    DEFAULT_MATERIALITY_ABS,
    DEFAULT_MATERIALITY_PCT,
    load_materiality,
)


async def test_load_materiality_returns_tenant_thresholds(db, tenant_a):
    # create_test_tenant seeds a TenantConfig with server defaults; override them.
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
    cfg.recon_materiality_abs = Decimal("250.00")
    cfg.recon_materiality_pct = Decimal("0.0500")
    await db.flush()

    mat_abs, mat_pct = await load_materiality(db, tenant_a.id)
    assert mat_abs == Decimal("250.00")
    assert mat_pct == Decimal("0.0500")


async def test_load_materiality_falls_back_to_defaults_when_no_config(db):
    # A tenant_id with no TenantConfig row → defaults.
    orphan_tenant_id = uuid.uuid4()
    mat_abs, mat_pct = await load_materiality(db, orphan_tenant_id)
    assert mat_abs == DEFAULT_MATERIALITY_ABS == Decimal("50")
    assert mat_pct == DEFAULT_MATERIALITY_PCT == Decimal("0.01")


async def test_recon_job_runner_delegates_to_shared(db, tenant_a):
    """ReconJobRunner._load_materiality still works and matches the shared helper."""
    from app.services.reconciliation.recon_job import ReconJobRunner

    runner = ReconJobRunner(db, tenant_a.id)
    assert await runner._load_materiality() == await load_materiality(db, tenant_a.id)


async def test_order_recon_job_delegates_to_shared(db, tenant_a):
    """OrderReconJob._load_materiality still works and matches the shared helper."""
    from app.services.reconciliation.order_recon_job import OrderReconJob

    job = OrderReconJob(db, tenant_a.id)
    assert await job._load_materiality() == await load_materiality(db, tenant_a.id)
