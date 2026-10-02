"""Freshness reaches admins through the existing digest without provider reads."""

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.tenant import Tenant
from app.models.transaction_ops import TransactionRun
from app.services import email_service, ops_digest
from app.services.transaction_ops import freshness_digest
from tests.conftest import create_test_user
from tests.test_ops_digest import _digest_rows
from tests.test_transaction_operational_status import NOW, seed

LATE = NOW + timedelta(days=2)


async def test_overdue_daily_scan_email_is_named_and_deduplicates_even_after_quiet_digest(db, admin_user):
    actor = admin_user[0]
    await seed(db, actor)
    sender = AsyncMock()

    await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[actor.tenant_id])
    for minutes in (5, 6):
        await ops_digest.run_ops_digest(
            db, now=LATE + timedelta(minutes=minutes), sender=sender, tenant_ids=[actor.tenant_id]
        )
    sender.assert_awaited_once()
    mail = sender.call_args.kwargs
    assert mail["to_email"] == actor.email
    assert "Framework UK: coverage overdue" in mail["text_body"]
    assert "Verified through 2026-09-26" in mail["text_body"]
    assert "expected 2026-09-28" in mail["text_body"]
    assert "/settings/ops-status" in mail["html_body"]
    rows = await _digest_rows(db, actor.tenant_id)
    assert [row.payload["delivery"] for row in rows] == ["sent", "nothing_to_report", "nothing_to_report"]
    assert rows[0].payload["counts"]["freshness"] == 1
    assert rows[-1].payload["freshness_keys"] == rows[0].payload["freshness_keys"]

    # A new missing daily window still reaches a human, even without progress.
    await ops_digest.run_ops_digest(db, now=LATE + timedelta(days=1), sender=sender, tenant_ids=[actor.tenant_id])
    assert sender.await_count == 2


@pytest.mark.parametrize("first_delivery", ["failed", "disabled"])
async def test_unsent_freshness_remains_retryable(db, admin_user, monkeypatch, first_delivery):
    actor = admin_user[0]
    await seed(db, actor)
    if first_delivery == "disabled":
        monkeypatch.setattr(settings, "OPS_DIGEST_EMAIL_ENABLED", False)
    sender = AsyncMock(side_effect=RuntimeError("email unavailable"))
    await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[actor.tenant_id])
    assert (await _digest_rows(db, actor.tenant_id))[-1].payload["delivery"] == first_delivery
    monkeypatch.setattr(settings, "OPS_DIGEST_EMAIL_ENABLED", True)
    working = AsyncMock()
    await ops_digest.run_ops_digest(db, now=LATE + timedelta(minutes=5), sender=working, tenant_ids=[actor.tenant_id])
    working.assert_awaited_once()


async def test_current_or_paused_daily_coverage_does_not_email(db, admin_user):
    actor = admin_user[0]
    config, _ = await seed(db, actor)
    sender = AsyncMock()
    await ops_digest.run_ops_digest(db, now=NOW, sender=sender, tenant_ids=[actor.tenant_id])
    config.schedule_enabled = False
    await db.flush()
    await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[actor.tenant_id])
    sender.assert_not_awaited()


async def test_partial_email_delivery_does_not_mark_freshness_fully_notified(db, admin_user):
    actor = admin_user[0]
    await seed(db, actor)
    await create_test_user(db, await db.get(Tenant, actor.tenant_id))
    partial = AsyncMock(side_effect=[None, RuntimeError("one mailbox unavailable")])
    await ops_digest.run_ops_digest(db, now=LATE, sender=partial, tenant_ids=[actor.tenant_id])
    assert (await _digest_rows(db, actor.tenant_id))[-1].payload["delivery"] == "partial"
    working = AsyncMock()
    await ops_digest.run_ops_digest(db, now=LATE + timedelta(minutes=5), sender=working, tenant_ids=[actor.tenant_id])
    assert working.await_count == 2  # Preserve the digest's existing partial-delivery retry policy.
    assert (await _digest_rows(db, actor.tenant_id))[-1].payload["delivery"] == "sent"


async def test_new_verified_daily_scan_clears_the_email_condition(db, admin_user):
    actor = admin_user[0]
    config, previous = await seed(db, actor)
    sender = AsyncMock()
    await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[actor.tenant_id])
    from app.services.transaction_ops.operational_status import operational_status

    snapshot = await operational_status(db, actor.tenant_id, daily_only=True, now=LATE)
    db.add(
        TransactionRun(
            tenant_id=actor.tenant_id,
            config_id=config.id,
            origin="schedule",
            work_key=uuid4().hex,
            params_json={
                "window_start": previous.params_json["window_end"],
                "window_end": snapshot["entities"][0]["coverage"]["expected_until"],
            },
            config_snapshot=previous.config_snapshot,
            status="finished",
            termination_reason="done",
            max_api_calls=100,
            max_orders=100,
            created_at=LATE,
            finished_at=LATE + timedelta(minutes=1),
            deadline_at=LATE + timedelta(minutes=15),
            progress_json=previous.progress_json,
        )
    )
    await db.flush()
    await ops_digest.run_ops_digest(db, now=LATE + timedelta(minutes=2), sender=sender, tenant_ids=[actor.tenant_id])
    sender.assert_awaited_once()
    rows = await _digest_rows(db, actor.tenant_id)
    assert rows[-1].payload["counts"]["freshness"] == 0 and rows[-1].payload["freshness_keys"] == []


async def test_foreign_digest_keys_cannot_suppress_or_disclose_this_tenants_alert(db, admin_user, admin_user_b):
    actor, other = admin_user[0], admin_user_b[0]
    await seed(db, actor)
    fresh = await freshness_digest.collect_freshness(db, actor.tenant_id, now=LATE)
    db.add(
        AuditEvent(
            tenant_id=other.tenant_id,
            category="ops",
            action="ops.digest",
            payload={"delivery": "sent", "freshness_keys": fresh["keys"], "until": LATE.isoformat()},
        )
    )
    await db.flush()
    sender = AsyncMock()
    await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[actor.tenant_id, other.tenant_id])
    sender.assert_awaited_once()
    assert sender.call_args.kwargs["to_email"] == actor.email
    assert (await _digest_rows(db, other.tenant_id))[-1].payload["counts"]["freshness"] == 0


async def test_collect_freshness_does_not_write_or_dispatch(db, admin_user):
    await seed(db, admin_user[0])
    statements = []
    conn = await db.connection()

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(conn.sync_connection, "before_cursor_execute", capture)
    try:
        result = await freshness_digest.collect_freshness(db, admin_user[0].tenant_id, now=LATE)
    finally:
        event.remove(conn.sync_connection, "before_cursor_execute", capture)
    assert len(result["alerts"]) == 1
    assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)


async def test_digest_lock_is_cross_connection_and_released_by_transaction(db, admin_user):
    async with db.bind.engine.connect() as left, db.bind.engine.connect() as right:
        assert await freshness_digest.claim_digest(left, admin_user[0].tenant_id)
        assert not await freshness_digest.claim_digest(right, admin_user[0].tenant_id)
        await left.rollback()
        assert await freshness_digest.claim_digest(right, admin_user[0].tenant_id)
        await right.rollback()


async def test_duplicate_busy_digest_skips_without_sending_or_auditing_success(db, admin_user, monkeypatch):
    monkeypatch.setattr(ops_digest, "claim_digest", AsyncMock(return_value=False))
    sender = AsyncMock()
    stats = await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[admin_user[0].tenant_id])
    sender.assert_not_awaited()
    assert stats["tenant_busy"] == 1 and stats["termination_reason"] == "budget"
    assert not await _digest_rows(db, admin_user[0].tenant_id)


async def test_freshness_beyond_first_page_is_not_silently_skipped(db, admin_user, monkeypatch):
    await seed(db, admin_user[0])
    from app.services.transaction_ops.operational_status import operational_status

    real = await operational_status(db, admin_user[0].tenant_id, daily_only=True, now=LATE)
    healthy = real["entities"][0] | {"freshness": {"state": "healthy"}}
    read = AsyncMock(
        side_effect=[
            {"entities": [healthy] * 50, "truncated": True},
            {"entities": real["entities"], "truncated": False},
        ]
    )
    monkeypatch.setattr(freshness_digest, "operational_status", read)
    result = await freshness_digest.collect_freshness(db, admin_user[0].tenant_id, now=LATE)
    assert len(result["alerts"]) == 1 and result["scopes_checked"] == 51
    assert read.call_args_list[1].kwargs["offset"] == 50


async def test_scope_cap_emails_incomplete_check_even_without_known_alerts(db, admin_user, monkeypatch):
    monkeypatch.setattr(freshness_digest, "MAX_SCOPES", 50)
    monkeypatch.setattr(
        freshness_digest,
        "operational_status",
        AsyncMock(return_value={"entities": [{"freshness": {"state": "healthy"}}] * 50, "truncated": True}),
    )
    sender = AsyncMock()
    await ops_digest.run_ops_digest(db, now=LATE, sender=sender, tenant_ids=[admin_user[0].tenant_id])
    sender.assert_awaited_once()
    assert "freshness check incomplete" in sender.call_args.kwargs["subject"]
    assert "additional schedules were not checked" in sender.call_args.kwargs["text_body"]


async def test_freshness_email_escapes_customer_controlled_names(db, admin_user, monkeypatch):
    await seed(db, admin_user[0])
    monkeypatch.setattr(email_service, "FRONTEND_URL", "https://staging.suitestudio.ai")
    digest = await ops_digest.collect(db, admin_user[0].tenant_id, now=LATE, since=NOW)
    digest["freshness_alerts"][0]["name"] = '<img src=x onerror="attack()">'
    _, text, html = ops_digest.render("Framework <script>alert(1)</script>", digest, since=NOW, until=LATE)
    assert "<img" in text and "<img" not in html and "<script>" not in html
    assert "&lt;img" in html and "https://staging.suitestudio.ai/settings/ops-status" in html
    assert digest["counts"]["freshness"] == 1
    assert len(digest["freshness_keys"][0]) == 64
