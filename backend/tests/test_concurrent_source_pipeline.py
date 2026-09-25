"""Real committed sessions prove fan-out isolation and the shared cursor fence."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.base import Base
from app.models.connection import Connection
from app.models.tenant import Tenant
from app.models.transaction_ops import TransactionFinding
from app.models.transaction_source_snapshot import TransactionSourceSnapshot
from app.services.transaction_ops import source_preparation, staged_netsuite
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.runner import run_investigation
from app.services.transaction_ops.source_reader import SourceReadError
from app.services.transaction_ops.staged_netsuite import StagedNetSuite as NativeStaged
from tests.conftest import create_test_tenant, create_test_user
from tests.test_transaction_chunk_runner import setup


@pytest.fixture
async def committed(monkeypatch):
    # Child sessions must see real committed seed data. Never run this fixture
    # on a remote/customer database; cleanup is limited to its random tenant.
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    assert make_url(url).host in {"localhost", "127.0.0.1", "postgres"}
    engine = create_async_engine(url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = None
    try:
        async with factory() as db:
            tenant = await create_test_tenant(db, slug="pipeline-test-" + uuid4().hex)
            tenant_id = tenant.id
            actor, _ = await create_test_user(db, tenant, role_name="admin")
            await db.commit()
            yield db, actor, factory
    finally:
        if tenant_id:
            # Teardown must still remove seed data if a cancellation test found
            # a broken pooled connection. Do not weaken the test's own pool.
            await engine.dispose()
            async with factory() as cleanup:
                # Production append-only triggers must remain in force in the
                # test itself. Only test-tenant teardown disables them locally.
                await cleanup.execute(text("SET LOCAL session_replication_role = replica"))
                for table in Base.metadata.tables.values():
                    if "tenant_id" in table.c:
                        await cleanup.execute(delete(table).where(table.c.tenant_id == tenant_id))
                await cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))
                await cleanup.commit()
        await engine.dispose()


async def parallel_setup(committed, monkeypatch, **options):
    db, actor, factory = committed
    run, refs, reader, writer, events = await setup(db, actor, monkeypatch, **options)
    connection = await db.get(Connection, UUID(run.config_snapshot["source_connection_id"]))
    connection.metadata_json = {**connection.metadata_json, "recon_prepare_concurrency": 4}
    await db.commit()
    target_started = asyncio.Event()
    base = staged_netsuite.StagedNetSuite

    class Prefetch(base):
        async def prefetch_orders(self, reference):
            events.append(("target_prefetch", reference))
            target_started.set()

    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", Prefetch)
    return run, refs, reader, writer, events, target_started


@pytest.mark.parametrize("phase", ["orders", "refunds", "destination"])
async def test_four_isolated_sources_overlap_target_then_commit_once(committed, monkeypatch, phase):
    db, actor, _ = committed
    run, refs, reader, writer, events, target = await parallel_setup(committed, monkeypatch, phase=phase)
    original = reader.side_effect
    started, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    sessions = set()

    async def source(branch, tenant, *args, **kwargs):
        nonlocal active, peak
        assert branch is not db
        assert UUID(await branch.scalar(text("SELECT current_setting('app.current_tenant_id')"))) == tenant
        sessions.add(id(branch))
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        try:
            await release.wait()
            return await original(branch, tenant, *args, **kwargs)
        finally:
            active -= 1

    reader.side_effect = source
    task = asyncio.create_task(run_investigation(db, actor.tenant_id, run.id))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert target.is_set() and active == 4  # Independent provider stages overlap.
        assert not writer.await_count
    finally:
        release.set()
    result = await task
    assert result["termination_reason"] == "done" and result["processed"] == 10
    assert peak == 4 and active == 0 and len(sessions) == 4
    assert reader.await_count == 10 and writer.await_count == 1
    assert events[-1] == ("commit", refs)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == 10 and current.api_calls_used == 20 and current.api_calls_held == 0
    assert current.progress_json["pending_refs"] == []
    assert current.progress_json["source_prepare_concurrency_peak"] == 4
    assert current.progress_json["pipeline_prepare_batches"] == 1
    assert current.progress_json["source_staged_hits"] == 10


@pytest.mark.parametrize("failure", ["provider", "cancel", "disabled"])
async def test_failure_drains_workers_preserves_snapshots_and_cursor(committed, monkeypatch, failure):
    db, actor, factory = committed
    run, refs, reader, writer, _, _ = await parallel_setup(committed, monkeypatch)
    run_id, tenant = run.id, actor.tenant_id
    original = reader.side_effect
    started, release = asyncio.Event(), asyncio.Event()
    active = calls = 0

    async def source(*args, **kwargs):
        nonlocal active, calls
        calls += 1
        active += 1
        if active == 4:
            started.set()
        try:
            await release.wait()
            if failure == "provider" and args[3] == refs[0]:
                raise RuntimeError("injected read failure")
            return await original(*args, **kwargs)
        finally:
            active -= 1

    reader.side_effect = source
    task = asyncio.create_task(run_investigation(db, tenant, run_id))
    await asyncio.wait_for(started.wait(), 5)
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if failure == "disabled":
            async with factory() as change:
                config = await state.get_config(change, tenant, run.config_id)
                config.enabled = False
                await change.commit()
        release.set()
        result = await task
        assert result["termination_reason"] == ("error" if failure == "provider" else "stall")
    assert active == 0
    assert calls == 4  # No new sends start after the failure/revocation.
    writer.assert_not_awaited()
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    assert current.progress_json["processed"] == 0
    assert current.api_calls_used == 20  # First attempts prepaid conservatively.
    snapshots = await db.scalar(
        select(func.count()).select_from(TransactionSourceSnapshot).where(TransactionSourceSnapshot.tenant_id == tenant)
    )
    assert snapshots == (0 if failure == "cancel" else 3 if failure == "provider" else 4)
    assert not await db.scalar(
        select(func.count()).select_from(TransactionFinding).where(TransactionFinding.tenant_id == tenant)
    )


@pytest.mark.parametrize("failure", ["transport", "timeout"])
async def test_transient_failure_retries_only_failed_work_with_new_spend(committed, monkeypatch, failure):
    db, actor, _ = committed
    run, refs, reader, writer, _, _ = await parallel_setup(committed, monkeypatch)
    original = reader.side_effect
    attempts = {}
    monkeypatch.setattr(source_preparation, "READ_TIMEOUT_SECONDS", 0.05)

    async def source(*args, **kwargs):
        ref = args[3]
        attempts[ref] = attempts.get(ref, 0) + 1
        if ref == refs[0] and attempts[ref] == 1:
            if failure == "timeout":
                await asyncio.Event().wait()
            raise SourceReadError("source_transport_failed")
        return await original(*args, **kwargs)

    reader.side_effect = source
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 10
    assert attempts == {ref: 2 if ref == refs[0] else 1 for ref in refs}
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == 22 and current.orders_used == 10
    assert current.progress_json["read_retry_count"] == 1
    assert current.progress_json["pending_refs"] == []
    writer.assert_awaited_once()


@pytest.mark.parametrize(
    "setting,expected",
    [
        (None, 1),
        ({}, 1),
        ({"recon_prepare_concurrency": 4}, 4),
        ({"recon_prepare_concurrency": True}, 1),
        ({"recon_prepare_concurrency": 7}, 1),
        ({"recon_prepare_concurrency": "4"}, 1),
    ],
)
async def test_source_pipeline_is_opt_in_and_fails_closed(db, admin_user, setting, expected):
    from tests.test_transaction_source_snapshot import seed

    actor, _ = admin_user
    connection, _ = await seed(db, actor.tenant_id)
    connection.metadata_json = setting
    await db.flush()
    assert await source_preparation.concurrency(db, actor.tenant_id, connection.id) == expected
    with pytest.raises(SourceReadError, match="source_not_found"):
        await source_preparation.concurrency(db, uuid4(), connection.id)


async def test_real_native_prefetch_settles_only_its_own_budget_and_counts_consumption(committed, monkeypatch):
    import hashlib
    from unittest.mock import AsyncMock

    from app.core.encryption import encrypt_credentials
    from app.services.transaction_ops import netsuite_bulk
    from app.services.transaction_ops.call_meter import note_call
    from tests.test_transaction_ops_runner import missing_target

    db, actor, _ = committed
    run, refs, reader, writer, _, _ = await parallel_setup(committed, monkeypatch)
    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", NativeStaged)
    connection = await db.get(Connection, UUID(run.config_snapshot["netsuite_connection_id"]))
    connection.encrypted_credentials = encrypt_credentials({"account_id": "6738075"})
    fingerprint = hashlib.sha256(connection.encrypted_credentials.encode()).hexdigest()
    await db.commit()
    active_sources, target_read = asyncio.Event(), asyncio.Event()
    original = reader.side_effect

    async def source(*args, **kwargs):
        active_sources.set()
        await asyncio.wait_for(target_read.wait(), 5)
        return await original(*args, **kwargs)

    async def target(*args, **kwargs):
        # Exact shared-parent accounting before any provider send.
        assert args[0] is db
        assert run.api_calls_used == 20 and run.api_calls_held == 35
        await asyncio.wait_for(active_sources.wait(), 5)
        note_call()
        target_read.set()
        values = {}
        for ref in refs:
            value = missing_target()
            value["observed_at"] = datetime.now(timezone.utc).isoformat()
            values[ref] = value
        return {"orders": values, "credential_fingerprint": fingerprint, "concurrency_peak": 1}

    reader.side_effect = source
    batch_reader = AsyncMock(side_effect=target)
    monkeypatch.setattr(netsuite_bulk, "read_orders", batch_reader)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 10
    batch_reader.assert_awaited_once()
    writer.assert_awaited_once()
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == 24 and current.api_calls_held == 0
    assert current.progress_json["metered_calls"] == 1
    assert current.progress_json["native_orders_batch_hits"] == 10  # Prefetch is not consumption.
    assert current.progress_json["native_orders_batches"] == 1


async def test_source_snapshot_batch_preserves_per_order_freshness_and_scope(db, admin_user):
    from copy import deepcopy
    from datetime import timedelta

    from app.models.canonical import Order
    from app.services.transaction_ops import source_snapshot
    from tests.test_transaction_source_snapshot import seed

    actor, _ = admin_user
    connection, evidence = await seed(db, actor.tenant_id)
    now = datetime.now(timezone.utc)
    version = now - timedelta(hours=1)
    refs = ["R100000001", "R100000002", "R100000003"]
    for i, ref in enumerate(refs):
        value = deepcopy(evidence)
        value["read_at"] = now.isoformat()
        value["orders"][0].update(number=ref, id=str(i + 1), updated_at=version.isoformat())
        assert await source_snapshot.save(db, actor.tenant_id, connection.id, ref, value, now=now)
    db.add(
        Order(
            tenant_id=actor.tenant_id,
            dedupe_key=str(uuid4()),
            source="solidus",
            source_id="1",
            source_connection_id=connection.id,
            order_number=refs[0],
            currency="USD",
            total_amount=100,
            status="complete",
            source_updated_at=version + timedelta(seconds=1),
        )
    )
    await db.flush()
    values = await source_snapshot.load_many(
        db,
        actor.tenant_id,
        connection.id,
        refs,
        since=now - timedelta(minutes=1),
        now=now,
        minimum_versions={refs[1]: version + timedelta(seconds=1)},
    )
    assert set(values) == {refs[2]}
    assert values[refs[2]]["orders"][0]["number"] == refs[2]
    assert values[refs[2]]["read_at"] == now.isoformat()
    assert values[refs[2]]["orders"][0]["total"] == "100"
    with pytest.raises(SourceReadError):
        await source_snapshot.load_many(db, uuid4(), connection.id, refs, since=now, now=now)


@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_nested_cancellation_waits_for_every_async_finalizer(cancel_parent):
    started, finalized = [], []
    ready = asyncio.Event()

    async def branch(index):
        try:
            started.append(index)
            if len(started) == 4:
                ready.set()
            await asyncio.Event().wait()
        finally:
            # Represents session rollback/close. A redundant second cancel
            # would interrupt this await and leave its connection unclosed.
            await asyncio.sleep(0.01)
            finalized.append(index)

    async def group():
        return await source_preparation.joined(*(branch(i) for i in range(4)))

    async def target():
        await ready.wait()
        if cancel_parent:
            await asyncio.Event().wait()
        raise RuntimeError("target failed")

    task = asyncio.create_task(source_preparation.joined(group(), target()))
    await asyncio.wait_for(ready.wait(), 2)
    if cancel_parent:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel_parent else RuntimeError):
        await task
    assert sorted(finalized) == [0, 1, 2, 3]


async def test_worker_connection_failure_falls_back_without_cancelling_target(committed, monkeypatch):
    from contextlib import asynccontextmanager

    db, actor, _ = committed
    run, refs, reader, writer, _, target = await parallel_setup(committed, monkeypatch)
    setups = 0

    @asynccontextmanager
    async def unavailable(**kwargs):
        nonlocal setups
        setups += 1
        raise OSError("injected connection unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(source_preparation, "worker_async_session", unavailable)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 10
    assert setups == 4 and target.is_set()
    assert reader.await_count == 10
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == 20 and current.api_calls_held == 0
    assert current.progress_json["pending_refs"] == []
    writer.assert_awaited_once()


async def test_target_failure_drains_inflight_sources_without_advancing_cursor(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, writer, _, _ = await parallel_setup(committed, monkeypatch)
    started = asyncio.Event()
    active = 0

    async def blocked_source(*args, **kwargs):
        nonlocal active
        active += 1
        if active == 4:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    async def target_failure(self, reference):
        await started.wait()
        raise RuntimeError("injected target failure")

    run_id, tenant = run.id, actor.tenant_id
    reader.side_effect = blocked_source
    monkeypatch.setattr(staged_netsuite.StagedNetSuite, "prefetch_orders", target_failure)
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "error" and active == 0
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs and current.progress_json["processed"] == 0
    writer.assert_not_awaited()


async def test_mixed_cached_chunk_prepays_only_missing_source_reads(committed, monkeypatch):
    from app.services.transaction_ops import source_snapshot

    db, actor, _ = committed
    run, refs, reader, writer, _, _ = await parallel_setup(committed, monkeypatch)
    connection_id = UUID(run.config_snapshot["source_connection_id"])
    for ref in refs[:3]:
        evidence = await reader.side_effect(db, actor.tenant_id, None, ref)
        await source_snapshot.save(db, actor.tenant_id, connection_id, ref, evidence, now=datetime.now(timezone.utc))
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 10
    assert reader.await_count == 7
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == 14 and current.progress_json["source_staged_hits"] == 7
    assert current.progress_json["source_snapshot_hits"] == 3
    writer.assert_awaited_once()
