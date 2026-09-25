from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.transaction_evidence_batch import TransactionEvidenceBatch
from app.services.transaction_ops.dependency_staging import DependencyStaging
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
from tests import test_transaction_ops_state_db as fixtures


@pytest.fixture
async def setup_state(db, admin_user):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service as state

    actor, _ = admin_user
    config = await fixtures.seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(
            evaluation_key="dependency-test",
            window_start=datetime(2026, 9, 8, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 10, tzinfo=timezone.utc),
        ),
        actor=actor,
    )
    return actor, config, run


async def staged(db, setup_state):
    actor, config, run = setup_state
    conn = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    conn.encrypted_credentials = encrypt_credentials({"account_id": config.netsuite_account_id, "access_token": "one"})
    await db.flush()
    progress = {}
    store = DependencyStaging(db, actor.tenant_id, run, run.config_snapshot, progress, lambda: run.created_at)
    return store, progress, conn


async def test_resume_keeps_original_inventory_through_token_refresh_and_long_pause(db, setup_state):
    store, progress, conn = await staged(db, setup_state)
    value = {"observed_at": "2026-09-10T00:00:01Z", "changes": [{"record_keys": [["transaction", "1"]]}]}
    ref = await store.put(value)
    actor, config, run = setup_state
    conn.encrypted_credentials = encrypt_credentials({"account_id": config.netsuite_account_id, "access_token": "two"})
    await db.flush()
    continuation = SimpleNamespace(id=uuid4(), params_json=deepcopy(run.params_json))
    resumed = DependencyStaging(
        db,
        actor.tenant_id,
        continuation,
        run.config_snapshot,
        deepcopy(progress),
        lambda: run.created_at + timedelta(days=1),
    )
    loaded = await resumed.get(ref)
    assert loaded == value
    loaded["changes"].clear()
    assert await resumed.get(ref) == value
    assert await store.put(value) == ref
    assert (
        len(
            list(
                await db.scalars(
                    select(TransactionEvidenceBatch).where(TransactionEvidenceBatch.kind == "dependencies")
                )
            )
        )
        == 1
    )


@pytest.mark.parametrize("failure", ["tenant", "config", "window", "root", "revoked"])
async def test_discovery_batch_cannot_cross_scope_or_revoked_connection(db, setup_state, failure):
    store, progress, conn = await staged(db, setup_state)
    ref = await store.put({"changes": []})
    actor, config, run = setup_state
    tenant, snapshot, params = actor.tenant_id, deepcopy(run.config_snapshot), deepcopy(run.params_json)
    if failure == "tenant":
        tenant = uuid4()
    if failure == "config":
        snapshot["subsidiary_id"] = "999"
    if failure == "window":
        params["window_end"] = "2026-09-11T00:00:00Z"
    if failure == "root":
        progress = {}
    if failure == "revoked":
        conn.status = "disconnected"
    await db.flush()
    resumed = DependencyStaging(
        db, tenant, SimpleNamespace(id=uuid4(), params_json=params), snapshot, progress, lambda: run.created_at
    )
    with pytest.raises(NetSuiteEvidenceError):
        await resumed.get(ref)


async def test_connection_revocation_also_blocks_worker_local_cache(db, setup_state):
    store, _, conn = await staged(db, setup_state)
    ref = await store.put({"changes": []})
    conn.status = "disconnected"
    await db.flush()
    with pytest.raises(NetSuiteEvidenceError, match="invalid_connection"):
        await store.get(ref)
