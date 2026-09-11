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


async def test_failed_daily_publications_have_backoff_and_finite_attempts(db, admin_user, monkeypatch):
    user, _ = admin_user
    source = await connection(db, user.tenant_id, metadata_json={"api_profile": "framework_sync"})
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
