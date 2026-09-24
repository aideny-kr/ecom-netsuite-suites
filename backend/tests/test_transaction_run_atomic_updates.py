"""Real commits and contending connections for reconciliation's hot state writes."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import set_tenant_context_session
from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.models.tenant import Tenant
from app.models.transaction_ops import TransactionConfig, TransactionFinding, TransactionRun
from app.models.user import User
from app.schemas.transaction_runs import ProgressUpdate
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_state_db as state_fixtures
from tests.conftest import _test_db_url

setup_state = state_fixtures.setup_state


@asynccontextmanager
async def committed_run():
    assert urlsplit(_test_db_url).hostname in {"localhost", "127.0.0.1", "postgres", "db"}
    engine = create_async_engine(_test_db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant, actor, source, target, config, run = (uuid4() for _ in range(6))
    try:
        async with factory() as db:
            db.add(Tenant(id=tenant, name="Atomic run test", slug=f"atomic-{tenant}"))
            await db.flush()
            db.add(
                User(
                    id=actor,
                    tenant_id=tenant,
                    email=f"{actor}@example.invalid",
                    full_name="Test",
                    hashed_password="unused",
                )
            )
            for identifier, provider in ((source, "solidus"), (target, "netsuite")):
                db.add(
                    Connection(
                        id=identifier,
                        tenant_id=tenant,
                        provider=provider,
                        label="Test",
                        status="active",
                        encrypted_credentials="unused",
                    )
                )
            await db.flush()
            db.add(
                TransactionConfig(
                    id=config,
                    tenant_id=tenant,
                    config_key=str(uuid4()),
                    name="Atomic test",
                    source_connection_id=source,
                    netsuite_connection_id=target,
                    netsuite_account_id="test",
                    subsidiary_id="1",
                    record_type="salesorder",
                    mapping_json={},
                    enabled=True,
                    schedule_enabled=False,
                    interval_minutes=60,
                    max_api_calls=10,
                    max_orders=10,
                    deadline_seconds=900,
                    created_by=actor,
                )
            )
            await db.flush()
            db.add(
                TransactionRun(
                    id=run,
                    tenant_id=tenant,
                    config_id=config,
                    work_key=str(uuid4()),
                    origin="manual",
                    params_json={},
                    config_snapshot={"deadline_seconds": 900},
                    max_api_calls=10,
                    max_orders=10,
                    deadline_at=datetime.now(timezone.utc) + timedelta(seconds=900),
                    progress_json={"processed": 0},
                )
            )
            await db.commit()
            token = await state.claim_run(db, tenant, run)
        yield factory, tenant, run, token
    finally:
        async with factory() as db:
            for model in (AuditEvent, TransactionFinding, TransactionRun, TransactionConfig):
                await db.execute(delete(model).where(model.tenant_id == tenant))
            # These are Solidus/NetSuite fixtures. Per-row deletion keeps the
            # provider guard active; bulk connection deletion requires a bypass.
            for connection in await db.scalars(select(Connection).where(Connection.tenant_id == tenant)):
                await db.delete(connection)
            await db.execute(delete(User).where(User.tenant_id == tenant))
            await db.execute(delete(Tenant).where(Tenant.id == tenant))
            await db.commit()
            assert await db.scalar(select(Tenant.id).where(Tenant.id == tenant)) is None
        await engine.dispose()


async def test_contending_holds_cannot_both_spend_the_remaining_budget():
    async with committed_run() as (factory, tenant, run, token):

        async def reserve():
            async with factory() as db:
                await set_tenant_context_session(db, tenant)
                return await state.reserve_budget(db, tenant, run, lease_token=token, api_calls=7, hold=True)

        assert sorted(await asyncio.gather(reserve(), reserve())) == [False, True]
        async with factory() as db:
            row = await state.get_run(db, tenant, run)
            assert (row.api_calls_used, row.api_calls_held, row.status) == (7, 0, "finished")
            assert row.termination_reason == "budget"


async def test_contending_settlements_cannot_release_the_same_hold_twice():
    async with committed_run() as (factory, tenant, run, token):
        async with factory() as db:
            assert await state.reserve_budget(db, tenant, run, lease_token=token, api_calls=5, hold=True)

        async def settle():
            async with factory() as db:
                return await state.settle_budget(db, tenant, run, lease_token=token, release=5, spent=2)

        results = await asyncio.gather(settle(), settle(), return_exceptions=True)
        assert results.count(True) == 1
        errors = [r for r in results if isinstance(r, state.StateError)]
        assert len(errors) == 1 and errors[0].code == "run_hold_exceeded"
        async with factory() as db:
            row = await state.get_run(db, tenant, run)
            assert (row.api_calls_used, row.api_calls_held) == (2, 0)


@pytest.mark.parametrize("operation", ["reserve", "settle", "progress"])
async def test_success_uses_one_run_statement_and_refreshes_existing_identity(db, setup_state, operation):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=5, hold=True)
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        if "transaction_ops_runs" in statement:
            statements.append(statement)

    engine = db.bind.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        if operation == "reserve":
            assert await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, orders=1)
            assert run.orders_used == 1
        elif operation == "settle":
            assert await state.settle_budget(db, actor.tenant_id, run.id, lease_token=token, release=5, spent=2)
            assert (run.api_calls_used, run.api_calls_held) == (2, 0)
        else:
            result = await state.update_progress(
                db, actor.tenant_id, run.id, ProgressUpdate(progress_json={"processed": 1}), lease_token=token
            )
            assert result is run and run.progress_json == {"processed": 1}
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1 and statements[0].startswith("UPDATE ")


@pytest.mark.parametrize("operation", ["reserve", "settle", "progress"])
async def test_expired_or_foreign_owner_cannot_change_hot_state(db, setup_state, tenant_b, operation):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=5, hold=True)
    original = (run.api_calls_used, run.api_calls_held, run.progress_json.copy())
    for tenant, owner, now, code in (
        (tenant_b.id, token, None, "not_found"),
        (actor.tenant_id, uuid4(), None, "run_lease_lost"),
        (actor.tenant_id, token, run.lease_until, "run_lease_lost"),
    ):
        options = {"lease_token": owner, "now": now}
        with pytest.raises(state.StateError, match=code):
            if operation == "reserve":
                await state.reserve_budget(db, tenant, run.id, api_calls=1, **options)
            elif operation == "settle":
                await state.settle_budget(db, tenant, run.id, release=5, spent=1, **options)
            else:
                await state.update_progress(
                    db, tenant, run.id, ProgressUpdate(progress_json={"processed": 99}), **options
                )
    row = await state.get_run(db, actor.tenant_id, run.id)
    assert (row.api_calls_used, row.api_calls_held, row.progress_json) == original


async def test_finding_and_cursor_commit_together_and_rollback_together(monkeypatch):
    async with committed_run() as (factory, tenant, run, token):
        checkpoint = ProgressUpdate(progress_json={"processed": 1, "pending_refs": ["R123456788"]})
        async with factory() as db:
            await state.record_finding(
                db, tenant, run, "R123456789", {"status": "test"}, lease_token=token, checkpoint=checkpoint
            )
        async with factory() as db:
            assert (await state.get_run(db, tenant, run)).progress_json == checkpoint.progress_json
            assert await db.scalar(select(TransactionFinding.id).where(TransactionFinding.run_id == run))

        async def fail_commit(db, tenant):
            await db.flush()
            raise RuntimeError("commit interrupted")

        monkeypatch.setattr(state, "_commit", fail_commit)
        async with factory() as db:
            with pytest.raises(RuntimeError, match="commit interrupted"):
                await state.record_finding(
                    db,
                    tenant,
                    run,
                    "R123456788",
                    {"status": "test"},
                    lease_token=token,
                    checkpoint=ProgressUpdate(progress_json={"processed": 2, "pending_refs": []}),
                )
            await db.rollback()
        async with factory() as db:
            assert (await state.get_run(db, tenant, run)).progress_json == checkpoint.progress_json
            assert not await db.scalar(
                select(TransactionFinding.id).where(
                    TransactionFinding.run_id == run, TransactionFinding.order_reference == "R123456788"
                )
            )


@pytest.mark.parametrize("amount", [2**31 - 1, 2**80])
async def test_oversized_reservation_keeps_budget_termination_semantics(db, setup_state, amount):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=3)
    assert not await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=amount)
    assert (run.api_calls_used, run.status, run.termination_reason) == (3, "finished", "budget")


async def test_oversized_settlement_is_rejected_without_driver_overflow(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    with pytest.raises(state.StateError, match="run_hold_exceeded"):
        await state.settle_budget(db, actor.tenant_id, run.id, lease_token=token, release=2**80, spent=0)
