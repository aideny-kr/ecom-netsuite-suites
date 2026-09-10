from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.canonical import Order
from app.services.ingestion import solidus_sync as sync
from app.services.transaction_ops.runner import run_investigation
from tests.test_solidus_ingestion import NOW, connection, source_order
from tests.test_transaction_ops_runner import NOW as RUN_NOW
from tests.test_transaction_ops_runner import State, missing_target
from tests.test_transaction_ops_runner import source_order as runner_source


async def test_old_refunded_order_enters_table_without_regressing_newer_source_version(db, admin_user):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id, metadata_json={"api_profile": "framework_sync"})
    old = source_order(updated_at=(NOW - timedelta(days=100)).isoformat(), total="100.123456")
    await sync.save_observed_order(db, user.tenant_id, conn.id, old, NOW)
    latest = source_order(total="200.123456", additional_tax_total=None)
    await sync.save_observed_order(db, user.tenant_id, conn.id, latest, NOW + timedelta(seconds=1))
    await sync.save_observed_order(db, user.tenant_id, conn.id, old, NOW + timedelta(seconds=2))
    rows = (await db.scalars(select(Order).where(Order.tenant_id == user.tenant_id))).all()
    assert len(rows) == 1
    assert rows[0].total_amount == Decimal("200.123456")
    assert rows[0].tax_amount is None
    assert rows[0].source_updated_at == NOW


@pytest.mark.parametrize("revoked", [False, True])
async def test_observation_cannot_write_for_foreign_or_revoked_connection(db, admin_user, admin_user_b, revoked):
    user = admin_user[0]
    conn = await connection(
        db,
        user.tenant_id if revoked else admin_user_b[0].tenant_id,
        metadata_json={"api_profile": "framework_sync"},
    )
    if revoked:
        conn.status = "revoked"
        await db.flush()
    with pytest.raises(sync.SolidusImportError, match="source_unavailable"):
        await sync.save_observed_order(db, user.tenant_id, conn.id, source_order(), NOW)
    assert await db.scalar(select(Order.id).where(Order.tenant_id == user.tenant_id)) is None


async def test_direct_investigation_mirrors_exact_scoped_source_before_native_read():
    from uuid import uuid4

    state = State()
    connection_id = uuid4()
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(connection_id))
    mirror = AsyncMock()
    source = runner_source()

    async def target(*args, **kwargs):
        mirror.assert_awaited_once()
        return missing_target()

    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: RUN_NOW,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=target,
        _order_mirror=mirror,
    )
    assert result["termination_reason"] == "done"
    assert mirror.call_args.args[1:4] == (state.tenant, connection_id, source["orders"][0])
