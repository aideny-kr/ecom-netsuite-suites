from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from sqlalchemy import event, select

from app.models.audit import AuditEvent
from app.services.ingestion import solidus_dispatch as dispatch
from tests.test_solidus_ingestion import connection


async def test_daily_refresh_is_durable_and_does_not_duplicate_a_manual_refresh(db, admin_user, monkeypatch):
    user, _ = admin_user
    source = await connection(db, user.tenant_id, metadata_json={"api_profile": "framework_sync"})
    publish = Mock()
    monkeypatch.setattr(dispatch, "publish_refresh", publish)
    first = await dispatch.queue_refresh(db, user.tenant_id, source.id, actor_id=user.id)
    second = await dispatch.queue_refresh(db, user.tenant_id, source.id, daily=True)
    assert first["status"] == second["status"] == "queued"
    assert second["already_running"] is True
    publish.assert_called_once()
    events = (
        await db.scalars(
            select(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id, AuditEvent.action == "sync.trigger")
        )
    ).all()
    assert len(events) == 1


async def sponsor(db, user, source):
    from app.schemas.transaction_runs import ConfigControl, ConfigCreate
    from app.services.transaction_ops import state_service as state
    from tests.test_transaction_ops_state_db import seed_config

    original = await seed_config(db, user.tenant_id, user)
    config = await state.create_config(
        db,
        user.tenant_id,
        ConfigCreate(
            name="Synthetic direct schedule",
            source_connection_id=source.id,
            netsuite_connection_id=original.netsuite_connection_id,
            netsuite_account_id=original.netsuite_account_id,
            subsidiary_id=original.subsidiary_id,
            mapping_json=original.mapping_json,
        ),
        actor=user,
    )
    await state.control_config(
        db, user.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=user
    )
    return config


async def test_failed_daily_publications_have_backoff_and_finite_attempts(db, admin_user, monkeypatch):
    user, _ = admin_user
    source = await connection(db, user.tenant_id, metadata_json={"api_profile": "framework_sync"})
    await sponsor(db, user, source)
    publish = Mock(side_effect=OSError("private broker URL"))
    monkeypatch.setattr(dispatch, "publish_refresh", publish)
    # Keep the injected scheduler clock AND inserted audit timestamps on the
    # same deterministic timeline. Wall-clock execution near midnight used to
    # move the fourth attempt into tomorrow, legitimately resetting its budget.
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)

    def stamp_attempt(mapper, connection, target):
        if target.tenant_id == user.tenant_id and target.category == "sync":
            target.timestamp = datetime.fromisoformat(target.payload["requested_at"])

    event.listen(AuditEvent, "before_insert", stamp_attempt)
    try:
        for offset in (0, 16, 32):
            result = await dispatch.queue_refresh(
                db, user.tenant_id, source.id, daily=True, now=now + timedelta(minutes=offset)
            )
            assert result["status"] == "failed"
            assert "private" not in str(result)
        assert (
            await dispatch.queue_refresh(db, user.tenant_id, source.id, daily=True, now=now + timedelta(minutes=48))
        )["status"] == "deferred"
        assert publish.call_count == 3
        tomorrow = now + timedelta(days=1)
        assert (await dispatch.queue_refresh(db, user.tenant_id, source.id, daily=True, now=tomorrow))[
            "status"
        ] == "failed"
        assert (
            await dispatch.queue_refresh(db, user.tenant_id, source.id, daily=True, now=tomorrow + timedelta(minutes=1))
        )["status"] == "deferred"
        assert publish.call_count == 4
    finally:
        event.remove(AuditEvent, "before_insert", stamp_attempt)


async def test_daily_queue_cannot_access_another_tenants_connection(db, admin_user, admin_user_b, monkeypatch):
    source = await connection(db, admin_user_b[0].tenant_id, metadata_json={"api_profile": "framework_sync"})
    publish = Mock()
    monkeypatch.setattr(dispatch, "publish_refresh", publish)
    result = await dispatch.queue_refresh(db, admin_user[0].tenant_id, source.id, daily=True)
    assert result["status"] == "unavailable"
    publish.assert_not_called()


async def test_revoked_creator_cannot_refresh_or_create_repeated_runs(db, admin_user, monkeypatch):
    from app.models.transaction_ops import TransactionRun
    from app.services.transaction_ops import scheduler
    from tests.conftest import enable_feature_flag

    user = admin_user[0]
    source = await connection(db, user.tenant_id, metadata_json={"api_profile": "framework_sync"})
    config = await sponsor(db, user, source)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, user.tenant_id, flag)
    user.is_active = False
    await db.commit()
    publish = Mock()
    monkeypatch.setattr(dispatch, "publish_refresh", publish)
    now = datetime.now(timezone.utc)
    for delta in (0, 61):
        stats = await scheduler.collect_due_runs(db, now + timedelta(minutes=delta))
        assert stats["created"] == stats["source_refreshes"] == 0
        assert stats["stalled"] == [{"config_id": str(config.id), "reason": "scheduled_detection_access_revoked"}]
    publish.assert_not_called()
    assert not (await db.scalars(select(TransactionRun))).all()


async def test_scheduled_refresh_rechecks_after_provider_read_without_persisting(db, admin_user, monkeypatch):
    import pytest

    from app.models.canonical import Order
    from app.services.ingestion import solidus_sync as sync
    from tests.test_solidus_ingestion import source_order

    user = admin_user[0]
    source = await connection(db, user.tenant_id, metadata_json={"api_profile": "framework_sync"})
    config = await sponsor(db, user, source)
    now = datetime.now(timezone.utc)

    async def read(*args, **kwargs):
        user.is_active = False
        await db.flush()
        return {
            "read_at": now.isoformat(),
            "orders": [source_order(updated_at=now.isoformat())],
            "next_page": None,
            "total_count": 1,
        }

    monkeypatch.setattr(sync, "read_framework_orders_page", read)
    with pytest.raises(sync.SolidusImportError, match="scheduled_detection_access_revoked"):
        await sync.sync_solidus_orders(db, user.tenant_id, source.id, now=now, schedule_config_id=config.id)
    assert not (await db.scalars(select(Order))).all()
