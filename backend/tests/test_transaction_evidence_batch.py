from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.transaction_evidence_batch import TransactionEvidenceBatch
from app.services.transaction_ops import evidence_batch as store
from tests import test_transaction_ops_state_db as fixtures

setup_state = fixtures.setup_state


async def seed(db, setup_state):
    actor, config, run = setup_state
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.encrypted_credentials = encrypt_credentials(
        {"account_id": config.netsuite_account_id, "access_token": "test"}
    )
    await db.flush()
    now = run.created_at + timedelta(seconds=10)
    fingerprint = await store.fingerprint(db, actor.tenant_id, connection.id, config.netsuite_account_id)
    data = {
        "credential_fingerprint": fingerprint,
        "orders": {"R123456789": {"observed_at": now.isoformat(), "amount": Decimal("1.2300")}},
    }
    context = store.context_hash(run.config_snapshot, "orders")
    identifier = await store.save(
        db, actor.tenant_id, run.id, "orders", context, run.config_snapshot, data, started_at=run.created_at, now=now
    )
    return actor, config, run, connection, identifier, context, now


async def test_durable_exact_values_copy_and_timestamp_survive_reloading(db, setup_state):
    actor, config, run, conn, identifier, context, now = await seed(db, setup_state)
    options = dict(since=run.created_at, now=now)
    data = await store.load(db, actor.tenant_id, identifier, "orders", context, run.config_snapshot, **options)
    assert data["R123456789"] == {"observed_at": now.isoformat(), "amount": "1.2300"}
    data["R123456789"]["amount"] = "999"
    second = await store.load(db, actor.tenant_id, identifier, "orders", context, run.config_snapshot, **options)
    assert second["R123456789"]["amount"] == "1.2300"
    assert await db.scalar(select(TransactionEvidenceBatch.id).where(TransactionEvidenceBatch.id == identifier))


@pytest.mark.parametrize(
    "failure", ["tenant", "credentials", "revoked", "account", "context", "kind", "expired", "new_scan", "future"]
)
async def test_incompatible_or_stale_batch_cannot_be_used(db, setup_state, failure):
    actor, config, run, conn, identifier, context, now = await seed(db, setup_state)
    tenant, kind, since, snapshot = actor.tenant_id, "orders", run.created_at, dict(run.config_snapshot)
    if failure == "tenant":
        tenant = uuid4()
    if failure == "credentials":
        conn.encrypted_credentials = encrypt_credentials(
            {"account_id": config.netsuite_account_id, "access_token": "changed"}
        )
    if failure == "revoked":
        conn.status = "disconnected"
    if failure == "account":
        snapshot["netsuite_account_id"] = "9999"
    if failure == "context":
        context = store.context_hash(snapshot, "dependencies")
    if failure == "kind":
        kind = "refunds"
    if failure == "expired":
        now += timedelta(days=2)
    if failure == "new_scan":
        since = now
    if failure == "future":
        now = run.created_at
    await db.flush()
    if failure in {"tenant", "revoked", "account"}:
        with pytest.raises(store.NetSuiteEvidenceError):
            await store.load(db, tenant, identifier, kind, context, snapshot, since=since, now=now)
    else:
        assert await store.load(db, tenant, identifier, kind, context, snapshot, since=since, now=now) is None


async def test_failed_collection_never_creates_batch_and_foreign_run_is_rejected(db, setup_state):
    actor, config, run = setup_state
    conn = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    conn.encrypted_credentials = encrypt_credentials({"account_id": config.netsuite_account_id})
    await db.flush()
    fingerprint = await store.fingerprint(db, actor.tenant_id, conn.id, config.netsuite_account_id)
    data = {"credential_fingerprint": fingerprint, "orders": {}}
    with pytest.raises(store.NetSuiteEvidenceError, match="invalid_batch_run"):
        await store.save(
            db,
            actor.tenant_id,
            uuid4(),
            "orders",
            "a" * 64,
            run.config_snapshot,
            data,
            started_at=run.created_at,
            now=run.created_at,
        )
    assert not list(await db.scalars(select(TransactionEvidenceBatch)))


async def test_migration_enforces_rls_and_composite_run_fk(db):
    row = (
        await db.execute(
            text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='transaction_evidence_batches'")
        )
    ).one()
    assert row == (True, True)
    policy = await db.scalar(
        text(
            "SELECT qual FROM pg_policies WHERE tablename='transaction_evidence_batches' AND policyname='tenant_isolation'"
        )
    )
    assert "get_current_tenant_id" in policy
    foreign = await db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='transaction_evidence_batches'::regclass AND contype='f'"
        )
    )
    assert "(tenant_id, run_id)" in foreign and "ON DELETE CASCADE" in foreign


async def test_replayed_identical_observation_is_idempotent_and_never_refreshes_times(db, setup_state):
    actor, config, run, conn, identifier, context, now = await seed(db, setup_state)
    fingerprint = await store.fingerprint(db, actor.tenant_id, conn.id, config.netsuite_account_id)
    data = {
        "credential_fingerprint": fingerprint,
        "orders": {"R123456789": {"observed_at": now.isoformat(), "amount": Decimal("1.2300")}},
    }
    assert (
        await store.save(
            db,
            actor.tenant_id,
            run.id,
            "orders",
            context,
            run.config_snapshot,
            data,
            started_at=run.created_at,
            now=now + timedelta(minutes=1),
        )
        == identifier
    )
    rows = list(await db.scalars(select(TransactionEvidenceBatch)))
    assert len(rows) == 1 and rows[0].completed_at == now
