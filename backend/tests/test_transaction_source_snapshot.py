"""Saved source evidence saves reads without widening scope or refreshing its age."""

import hashlib
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.core.encryption import encrypt_credentials
from app.models.canonical import Order
from app.models.connection import Connection
from app.models.transaction_source_snapshot import TransactionSourceSnapshot as Snapshot
from app.services.transaction_ops import source_snapshot as snapshots
from app.services.transaction_ops.runner import run_investigation
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_transaction_ops_runner import NOW, REF, State, missing_target, source_order


async def seed(db, tenant):
    conn = Connection(
        tenant_id=tenant,
        provider="solidus",
        label="Snapshot test",
        status="active",
        metadata_json={"api_profile": "framework_sync"},
        encrypted_credentials=encrypt_credentials(
            {
                "base_url": "https://private-direct-access.frame.work/api/",
                "auth_type": "api_key",
                "header_name": "X-Store-Token",
                "token": "test",
                "api_profile": "framework_sync",
            }
        ),
    )
    db.add(conn)
    await db.flush()
    evidence = source_order()
    evidence.update(
        source_transport="solidus_direct",
        connection_id=str(conn.id),
        _connection_fingerprint=hashlib.sha256(conn.encrypted_credentials.encode()).hexdigest(),
    )
    return conn, evidence


async def read(db, tenant, conn, **options):
    return await snapshots.load(db, tenant, conn.id, REF, since=NOW - timedelta(minutes=1), now=NOW, **options)


async def test_saved_detail_survives_sessions_with_exact_money_age_and_tax_geography(db, admin_user):
    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    evidence["orders"][0].update(
        tax_jurisdiction={"country_iso": "GB", "zipcode": "SW1", "state": {"name": "London"}},
        email="private@example.com",
        password="never-store",
    )
    assert await snapshots.save(db, user.tenant_id, conn.id, REF, evidence, now=NOW)
    db.expunge_all()
    result = await read(db, user.tenant_id, conn)
    assert result["read_at"] == evidence["read_at"]
    assert result["orders"][0]["total"] == "100"
    assert result["orders"][0]["line_items"] == evidence["orders"][0]["line_items"]
    assert result["orders"][0]["tax_jurisdiction"] == evidence["orders"][0]["tax_jurisdiction"]
    assert "private@example.com" not in str(result) and "never-store" not in str(result)
    result["orders"][0]["total"] = "999"
    assert (await read(db, user.tenant_id, conn))["orders"][0]["total"] == "100"


@pytest.mark.parametrize(
    "reason", ["new_cycle", "expired", "future", "replica", "mirror", "credentials", "revoked", "tenant", "connection"]
)
async def test_invalidated_evidence_is_not_reused(db, admin_user, reason):
    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    await snapshots.save(db, user.tenant_id, conn.id, REF, evidence, now=NOW)
    since, now, minimum = NOW - timedelta(minutes=1), NOW, None
    tenant, connection_id = user.tenant_id, conn.id
    if reason == "new_cycle":
        since, now = NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)
    elif reason == "expired":
        now += timedelta(days=2)
    elif reason == "future":
        now -= timedelta(seconds=1)
    elif reason == "replica":
        minimum = NOW
    elif reason == "mirror":
        db.add(
            Order(
                tenant_id=tenant,
                dedupe_key=str(uuid4()),
                source="solidus",
                source_id="1",
                source_connection_id=conn.id,
                order_number=REF,
                currency="USD",
                total_amount=100,
                status="complete",
                source_updated_at=NOW,
            )
        )
    elif reason == "credentials":
        conn.encrypted_credentials = encrypt_credentials(
            {
                "base_url": "https://private-direct-access.frame.work/api/",
                "auth_type": "api_key",
                "header_name": "X-Store-Token",
                "token": "rotated",
                "api_profile": "framework_sync",
            }
        )
    elif reason == "revoked":
        conn.status = "revoked"
    elif reason == "tenant":
        tenant = uuid4()
    elif reason == "connection":
        other, _ = await seed(db, tenant)
        connection_id = other.id
    await db.flush()
    if reason in {"revoked", "tenant"}:
        with pytest.raises(SourceReadError):
            await snapshots.load(db, tenant, connection_id, REF, since=since, now=now)
    else:
        assert (
            await snapshots.load(db, tenant, connection_id, REF, since=since, now=now, minimum_version=minimum) is None
        )


async def test_late_older_result_cannot_replace_newer_evidence_and_rotation_during_read_is_not_saved(db, admin_user):
    user, _ = admin_user
    conn, old = await seed(db, user.tenant_id)
    new = deepcopy(old)
    new["orders"][0].update(total="101", updated_at=NOW.isoformat())
    new["read_at"] = (NOW + timedelta(seconds=1)).isoformat()
    await snapshots.save(db, user.tenant_id, conn.id, REF, new, now=NOW + timedelta(seconds=1))
    await snapshots.save(db, user.tenant_id, conn.id, REF, old, now=NOW + timedelta(seconds=2))
    row = await db.scalar(select(Snapshot).where(Snapshot.tenant_id == user.tenant_id))
    assert row.evidence_json["evidence"]["orders"][0]["total"] == "101"
    new["_connection_fingerprint"] = "0" * 64
    assert await snapshots.save(db, user.tenant_id, conn.id, REF, new, now=NOW + timedelta(seconds=2)) is False
    assert await db.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.tenant_id == user.tenant_id)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "window"},
        {"page_complete": False},
        {"orders": []},
        {"orders": 1},
        {"source_transport": "celigo"},
        {"read_at": "invalid"},
        {"connection_id": str(uuid4())},
    ],
)
async def test_invalid_or_page_evidence_cannot_seed_snapshot(db, admin_user, change):
    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    evidence.update(change)
    assert await snapshots.save(db, user.tenant_id, conn.id, REF, evidence, now=NOW) is False


async def test_rls_snapshot_read_and_write_use_real_non_bypass_role(db, admin_user):
    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    await snapshots.save(db, user.tenant_id, conn.id, REF, evidence, now=NOW)
    role = "snapshot_test_" + uuid4().hex
    await db.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    await db.execute(text(f"GRANT SELECT, INSERT ON transaction_source_snapshots TO {role}"))
    try:
        await db.execute(text(f"SET LOCAL ROLE {role}"))
        from app.core.database import set_tenant_context

        await set_tenant_context(db, user.tenant_id)
        assert len((await db.execute(text("SELECT id FROM transaction_source_snapshots"))).all()) == 1
        await set_tenant_context(db, uuid4())
        assert (await db.execute(text("SELECT id FROM transaction_source_snapshots"))).all() == []
        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.begin_nested():
                await db.execute(
                    text("""INSERT INTO transaction_source_snapshots
                    (id, tenant_id, connection_id, order_reference, connection_fingerprint,
                     observed_at, source_updated_at, evidence_json)
                    VALUES (:id, :tenant, :connection, 'R999999999', 'fingerprint', now(), now(), '{}')"""),
                    {"id": uuid4(), "tenant": user.tenant_id, "connection": conn.id},
                )
    finally:
        await db.execute(text("RESET ROLE"))
        await db.execute(text(f"DROP OWNED BY {role}"))
        await db.execute(text(f"DROP ROLE {role}"))


def test_scan_floor_preserves_continuations_but_not_new_scans_or_write_recovery():
    run = SimpleNamespace(
        origin="manual", params_json={"window_start": "start", "window_end": "end"}, progress_json={}, created_at=NOW
    )
    assert snapshots.scan_floor(run, NOW) == NOW
    run.created_at = NOW + timedelta(hours=1)
    run.progress_json = {"continuation_started_at": NOW.isoformat()}
    assert snapshots.scan_floor(run, NOW + timedelta(hours=1)) == NOW
    run.origin = "recovery"
    assert snapshots.scan_floor(run, NOW + timedelta(hours=1)) is None
    run.origin = "manual"
    run.params_json["order_references"] = [REF]
    assert snapshots.scan_floor(run, NOW + timedelta(hours=1)) is None


@pytest.mark.parametrize("hit", [True, False])
async def test_scan_reuses_detail_without_charging_remote_calls_or_proposing_writes(monkeypatch, hit):
    state = State(window=True)
    state.run.created_at = NOW - timedelta(seconds=1)
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(uuid4()))
    state.run.config_snapshot["mapping_json"]["action_mode"] = "propose_actions"
    state.run.progress_json = {
        "pending_refs": [REF],
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": True,
    }
    cached = source_order()
    load = AsyncMock(return_value=cached if hit else None)
    save = AsyncMock()
    source = AsyncMock(return_value=cached)
    monkeypatch.setattr(snapshots, "load", load)
    monkeypatch.setattr(snapshots, "save", save)
    state.propose = AsyncMock()
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=source,
        _target_reader=AsyncMock(return_value=missing_target()),
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == "done"
    assert source.await_count == (0 if hit else 1)
    assert save.await_count == (0 if hit else 1)
    assert state.events[0] == ("reserve", 0 if hit else 2, 1)
    if hit:
        state.propose.assert_not_awaited()
        assert state.run.progress_json["source_snapshot_hits"] == 1
    assert state.reports[REF]["source_provenance"]["read_at"] == cached["read_at"]


async def test_four_entity_scans_fetch_unscoped_source_once_and_keep_entity_boundaries(db, admin_user):
    from tests.test_metabase_replica_reader import BINDING

    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    source = AsyncMock(return_value=evidence)
    target = AsyncMock(return_value=missing_target())
    mirror = AsyncMock()
    for subsidiary in ("2", "1", "3", "4"):
        state = State(window=True)
        state.tenant = user.tenant_id
        state.run.created_at = NOW - timedelta(seconds=1)
        state.run.config_snapshot.update(
            source_step_id=None,
            source_connection_id=str(conn.id),
            subsidiary_id=subsidiary,
        )
        state.run.config_snapshot["mapping_json"]["metabase_replica"] = BINDING
        state.run.progress_json = {
            "pending_refs": [REF],
            "unscoped_replica_refs": [REF],
            "scan_complete": True,
            "refund_scan_complete": True,
            "destination_scan_complete": True,
        }
        result = await run_investigation(
            db,
            state.tenant,
            state.run_id,
            _state=state,
            _source_reader=source,
            _target_reader=target,
            _order_mirror=mirror,
            _enabled=AsyncMock(return_value=True),
            _clock=lambda: NOW,
        )
        assert result["termination_reason"] == "done"
        assert state.run.progress_json["processed"] == (1 if subsidiary == "1" else 0)
        assert state.run.progress_json["outside_scope"] == (0 if subsidiary == "1" else 1)
    source.assert_awaited_once()
    target.assert_awaited_once()
    mirror.assert_awaited_once()
    assert mirror.call_args.kwargs == {"reused": True}
    assert mirror.call_args.args[4] == NOW


async def test_target_timeout_restart_reuses_saved_source_without_skipping_unread_order(db, admin_user):
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    state = State(window=True)
    state.tenant = user.tenant_id
    state.run.created_at = NOW - timedelta(seconds=1)
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(conn.id))
    state.run.progress_json = {
        "pending_refs": [REF],
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": True,
        "read_retry_count": 3,
    }
    source = AsyncMock(return_value=evidence)
    target = AsyncMock(side_effect=NetSuiteEvidenceError("read_transport_failed"))
    args = dict(
        _state=state,
        _source_reader=source,
        _target_reader=target,
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    first = await run_investigation(db, state.tenant, state.run_id, **args)
    assert first["termination_reason"] == "budget"
    assert state.run.progress_json["pending_refs"] == [REF] and not state.reports
    target.side_effect = None
    target.return_value = missing_target()
    second = await run_investigation(db, state.tenant, state.run_id, **args)
    assert second["termination_reason"] == "done" and REF in state.reports
    source.assert_awaited_once()
    assert target.await_count == 2


async def test_newer_direct_source_page_forces_detail_refresh(db, admin_user):
    user, _ = admin_user
    conn, old = await seed(db, user.tenant_id)
    await snapshots.save(db, user.tenant_id, conn.id, REF, old, now=NOW)
    state = State(window=True)
    state.tenant = user.tenant_id
    state.run.created_at = NOW - timedelta(seconds=1)
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(conn.id))
    state.run.progress_json = {"refund_scan_complete": True, "destination_scan_complete": True}
    newer = deepcopy(old)
    newer["orders"][0].update(updated_at=NOW.isoformat(), total="101")
    page = AsyncMock(
        return_value={
            "orders": newer["orders"],
            "page_complete": True,
            "page": 1,
            "total_count": 1,
            "next_page": None,
        }
    )
    source = AsyncMock(return_value=newer)
    result = await run_investigation(
        db,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=source,
        _page_reader=page,
        _target_reader=AsyncMock(return_value=missing_target()),
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == "done"
    source.assert_awaited_once()
    assert state.reports[REF]["source"]["total"] == "101"
    assert not state.run.progress_json.get("source_snapshot_hits")


async def test_reused_mirror_retains_original_age_and_cannot_overwrite_newer_order(db, admin_user):
    from app.models.audit import AuditEvent
    from app.services.ingestion.solidus_sync import save_observed_order

    user, _ = admin_user
    conn, evidence = await seed(db, user.tenant_id)
    order = evidence["orders"][0]
    await save_observed_order(db, user.tenant_id, conn.id, order, NOW, reused=True)
    row = await db.scalar(select(Order).where(Order.tenant_id == user.tenant_id))
    assert row.raw_data["observed_at"] == NOW.isoformat()
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.tenant_id == user.tenant_id, AuditEvent.action == "solidus.order.observation_reused"
        )
    )
    assert audit.payload["observed_at"] == NOW.isoformat()
    newer = {**order, "updated_at": (NOW + timedelta(seconds=1)).isoformat(), "total": "101"}
    await save_observed_order(db, user.tenant_id, conn.id, newer, NOW + timedelta(seconds=2))
    await save_observed_order(db, user.tenant_id, conn.id, order, NOW, reused=True)
    await db.refresh(row)
    assert str(row.total_amount) == "101.000000"
    assert row.raw_data["observed_at"] == (NOW + timedelta(seconds=2)).isoformat()
