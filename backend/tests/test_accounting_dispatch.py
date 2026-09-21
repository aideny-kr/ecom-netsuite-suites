"""Committed PostgreSQL tests: real outbox, contention, restart and scoped authority.

Only the external correction boundary is simulated; no NetSuite calls or models.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.models.tenant import Tenant
from app.models.user import User
from app.services.transaction_ops import accounting_dispatch as dispatch
from app.services.transaction_ops import accounting_group as group
from tests.test_accounting_group import group_fixture


@asynccontextmanager
async def seeded_group(count=5, action="approve"):
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    assert urlsplit(url).hostname in {"localhost", "127.0.0.1", "postgres", "db"}, (
        "Committed test requires local fixture DB"
    )
    engine = create_async_engine(url, pool_size=12, max_overflow=5, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    so, identities = group_fixture(count)
    tenant_id, actor_id = identities.tenant_id, identities.user_id
    parent_id = uuid4()
    try:
        async with factory() as db:
            db.add(Tenant(id=tenant_id, name="Dispatch test", slug=f"dispatch-{tenant_id}"))
            await db.flush()
            db.add(
                User(
                    id=actor_id,
                    tenant_id=tenant_id,
                    email=f"{actor_id}@example.invalid",
                    full_name="Test reviewer",
                    hashed_password="unused",
                )
            )
            await db.flush()
            session = ChatSession(id=identities.id, tenant_id=tenant_id, user_id=actor_id, title="Dispatch fixture")
            db.add(session)
            await db.flush()
            parent = ChatMessage(
                id=parent_id,
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content="test",
                structured_output=so,
            )
            db.add(parent)
            for m in so["accounting_group"]["members"]:
                db.add(
                    ChatMessage(
                        id=UUID(m["confirmation_id"]),
                        tenant_id=tenant_id,
                        session_id=session.id,
                        role="assistant",
                        content="test",
                        structured_output=m["card"],
                    )
                )
            await db.flush()
            assert len(group.validate_manifest(so, str(session.id))) == count
            accepted = await dispatch.accept_dispatch(
                db, tenant_id, session, parent, so, action, actor_id, "test-dispatch"
            )
            from app.services.chat.orchestrator import _cas_claim_write_confirmation

            assert await _cas_claim_write_confirmation(db, parent, accepted, "executing")
        yield factory, tenant_id, parent_id, so
    finally:
        async with factory() as db:
            for model in (AuditEvent, ChatMessage, ChatSession, User):
                await db.execute(delete(model).where(model.tenant_id == tenant_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
            assert not await db.scalar(select(Tenant.id).where(Tenant.id == tenant_id))
        await engine.dispose()


def simulated_executor(calls, *, fail=None, gate=None):
    async def invoke(db, tenant, parent, auth, member):
        identifier = member["confirmation_id"]
        calls.append(identifier)
        if gate:
            gate.set()
            await asyncio.Event().wait()
        if identifier == fail:
            raise ValueError("evidence_changed")
        child = await dispatch.message(db, tenant, UUID(identifier))
        assert child.structured_output["status"] == "pending"
        child.structured_output = {
            **child.structured_output,
            "status": "rejected" if auth["action"] == "reject" else "approved",
            "accounting_verification": {"status": "verified"},
        }
        await db.commit()

    return invoke


async def test_all_55_verified_orders_drain_across_slices_and_duplicate_delivery(monkeypatch):
    async with seeded_group(55) as (factory, tenant, parent, so):
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            first = await dispatch.run_slice(db, tenant, parent)
            assert first == {"status": "queued", "remaining": 25, "orders": 55}
        async with factory() as db:
            second = await dispatch.run_slice(db, tenant, parent)
            assert second == {"status": "finished", "remaining": 0, "orders": 55}
            value = (await dispatch.message(db, tenant, parent)).structured_output
            states = list(value["accounting_group_dispatch"]["members"].values())
            assert sum(r["status"] == "verified" for r in states) == 55
            assert sum(r["status"] == "needs_review" for r in states) == 0
            assert value["status"] == "approved"
            events = list(
                await db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenant, AuditEvent.action == "accounting_group.case.completed"
                    )
                )
            )
            assert len(events) == 55 and all(e.actor_id for e in events)
            assert len(await dispatch.candidates(db, tenant)) == 0
        async with factory() as db:
            assert (await dispatch.run_slice(db, tenant, parent))["status"] == "not_pending"
        assert len(calls) == len(set(calls)) == 55


async def test_cancellation_reserves_only_started_children_and_restart_never_resends_them(monkeypatch):
    async with seeded_group(8) as (factory, tenant, parent, so):
        calls, started = [], asyncio.Event()
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls, gate=started))
        async with factory() as db:
            task = asyncio.create_task(dispatch.run_slice(db, tenant, parent))
            await asyncio.wait_for(started.wait(), 5)
            async with factory() as other:
                assert (await dispatch.run_slice(other, tenant, parent))["status"] == "busy"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        interrupted = set(calls)
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            assert (await dispatch.run_slice(db, tenant, parent))["status"] == "finished"
            value = (await dispatch.message(db, tenant, parent)).structured_output
            for identifier in interrupted:
                assert value["accounting_group_dispatch"]["members"][identifier]["status"] == "needs_review"
            assert all(
                v["status"] not in {"queued", "dispatching"}
                for v in value["accounting_group_dispatch"]["members"].values()
            )
        assert len(calls) == len(set(calls))  # includes attempts cancelled before any simulated write


@pytest.mark.parametrize("tamper", ["tenant", "actor", "manifest", "member", "audit", "action"])
async def test_dispatch_requires_tenant_bound_recorded_human_authority(monkeypatch, tamper):
    async with seeded_group(2) as (factory, tenant, parent, so):
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            if tamper == "tenant":
                assert (await dispatch.run_slice(db, uuid4(), parent))["status"] == "unavailable"
            else:
                message = await dispatch.message(db, tenant, parent)
                value = deepcopy(message.structured_output)
                work = value["accounting_group_dispatch"]
                if tamper == "audit":
                    work["audit_id"] = str(uuid4())
                elif tamper == "member":
                    work["authorization"]["members"][0]["card_digest"] = "forged"
                else:
                    work["authorization"][
                        {"actor": "actor_id", "manifest": "manifest_digest", "action": "action"}[tamper]
                    ] = str(uuid4())
                message.structured_output = value
                await db.commit()
                assert (await dispatch.run_slice(db, tenant, parent))["status"] == "needs_review"
                assert not await dispatch.candidates(db, tenant)
        assert not calls


async def test_known_execution_is_not_retried_when_its_dispatch_receipt_was_lost(monkeypatch):
    async with seeded_group(4) as (factory, tenant, parent, so):
        first = so["accounting_group"]["members"][0]["confirmation_id"]
        async with factory() as db:
            m = await dispatch.message(db, tenant, parent)
            value = deepcopy(m.structured_output)
            value["accounting_group_dispatch"]["members"][first] = {"status": "dispatching"}
            m.structured_output = value
            child = await dispatch.message(db, tenant, UUID(first))
            child.structured_output = {
                **child.structured_output,
                "status": "executing",
                "accounting_execution": {"receipt": None},
            }
            await db.commit()
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            await dispatch.run_slice(db, tenant, parent)
            value = (await dispatch.message(db, tenant, parent)).structured_output
            assert value["accounting_group_dispatch"]["members"][first]["status"] == "verification_pending"
        assert calls == []


@pytest.mark.parametrize("verification", [{"status": "needs_review"}, {}, None])
def test_approved_without_independent_verification_is_never_a_verified_result(verification):
    result = dispatch.outcome({"status": "approved", "accounting_verification": verification}, "approve")
    assert result["status"] == "verification_pending"
    assert "not be resent" in result["reason"]


async def test_group_rejection_drains_all_children_without_financial_execution(monkeypatch):
    async with seeded_group(5, action="reject") as (factory, tenant, parent, so):
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            result = await dispatch.run_slice(db, tenant, parent)
            assert result["status"] == "finished"
            assert (await dispatch.message(db, tenant, parent)).structured_output["status"] == "rejected"
        assert len(calls) == 5


@pytest.mark.parametrize("mode", ["after_reservation", "after_write"])
async def test_real_process_kill_blocks_untouched_orders_without_replaying_a_reserved_write(
    monkeypatch, tmp_path, mode
):
    import json
    import os
    import sys
    from pathlib import Path

    async with seeded_group(8) as (factory, tenant, parent, so):
        ready = tmp_path / "ready.json"
        script = Path(__file__).parent / "helpers" / "group_dispatch_crash_worker.py"
        # Backend cwd/PYTHONPATH are inherited. Credentials remain in the normal
        # local test configuration, never argv, output, or the readiness file.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            str(tenant),
            str(parent),
            str(ready),
            mode,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(15):
                while not ready.exists():
                    if process.returncode is not None:
                        raise AssertionError((await process.stderr.read()).decode())
                    await asyncio.sleep(0.02)
            first = json.loads(ready.read_text())["confirmation_id"]
            process.kill()
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            result = await dispatch.run_slice(db, tenant, parent)
            assert result["status"] == "finished" and result["remaining"] == 0
            value = (await dispatch.message(db, tenant, parent)).structured_output
            states = value["accounting_group_dispatch"]["members"]
            assert first not in calls
            assert states[first]["status"] == ("verification_pending" if mode == "after_write" else "needs_review")
            assert all(m["status"] not in {"queued", "dispatching"} for m in states.values())
            writes = list(
                await db.scalars(
                    select(AuditEvent.id).where(
                        AuditEvent.tenant_id == tenant, AuditEvent.action == "test.only.simulated_native_write"
                    )
                )
            )
            assert len(writes) == (1 if mode == "after_write" else 0)
        assert calls == []


async def test_capacity_defers_only_an_unchanged_unattempted_child_and_is_bounded(monkeypatch):
    async with seeded_group(2) as (factory, tenant, parent, so):
        calls = []
        first = so["accounting_group"]["members"][0]["confirmation_id"]
        normal = simulated_executor(calls)

        async def saturated(db, tenant_id, parent_id, auth, member):
            if member["confirmation_id"] == first:
                calls.append(first)
                return {"code": "accounting_capacity_busy", "reason": "Account busy; no write submitted."}
            return await normal(db, tenant_id, parent_id, auth, member)

        monkeypatch.setattr(dispatch, "invoke_child", saturated)
        for attempt in range(6):
            async with factory() as db:
                result = await dispatch.run_slice(db, tenant, parent)
                assert result["status"] == ("waiting" if attempt < 5 else "finished")
        async with factory() as db:
            value = (await dispatch.message(db, tenant, parent)).structured_output
            states = value["accounting_group_dispatch"]["members"]
            assert states[first]["status"] == "needs_review" and states[first]["attempts"] == 6
            assert sum(m["status"] == "verified" for m in states.values()) == 1
        assert len(calls) == 7


async def test_broker_outage_preserves_outbox_for_collector_and_database_clock(monkeypatch):
    from datetime import datetime, timezone

    from app.services.transaction_ops import action_scheduler

    async with seeded_group(1) as (factory, tenant, parent, so):

        def unavailable(*args, **kwargs):
            raise OSError("broker_unavailable")

        monkeypatch.setattr(action_scheduler, "publish_action", unavailable)
        assert (await dispatch.publish(tenant, parent))["dispatch_failed"] == 1
        async with factory() as db:
            assert await dispatch.candidates(db, tenant, datetime(2000, 1, 1, tzinfo=timezone.utc)) == [parent]
            assert await dispatch.candidates(db, uuid4()) == []


@pytest.mark.parametrize("uncertain", ["verification_pending", "exception"])
async def test_unconfirmed_outcome_stops_queued_members_but_running_members_finish(monkeypatch, uncertain):
    async with seeded_group(35) as (factory, tenant, parent, so):
        first = so["accounting_group"]["members"][0]["confirmation_id"]
        calls, started, release = [], asyncio.Event(), asyncio.Event()

        async def invoke(db, tenant_id, parent_id, auth, member):
            identifier = member["confirmation_id"]
            calls.append(identifier)
            if len(calls) == 3:
                started.set()
            await asyncio.wait_for(started.wait(), 5)
            if identifier == first:
                if uncertain == "exception":
                    raise RuntimeError("uncertain provider outcome")
                child = await dispatch.message(db, tenant, UUID(identifier))
                child.structured_output = {
                    **child.structured_output,
                    "status": "indeterminate",
                    "accounting_execution": {"receipt": None},
                }
                await db.commit()
            else:
                await asyncio.wait_for(release.wait(), 5)
                await simulated_executor([])(db, tenant_id, parent_id, auth, member)

        monkeypatch.setattr(dispatch, "invoke_child", invoke)
        async with factory() as db:
            task = asyncio.create_task(dispatch.run_slice(db, tenant, parent))
            try:
                async with asyncio.timeout(5):
                    while True:
                        async with factory() as other:
                            value = (await dispatch.message(other, tenant, parent)).structured_output
                            if value["accounting_group_dispatch"].get("stopped_after"):
                                break
                        await asyncio.sleep(0.01)
                states = value["accounting_group_dispatch"]["members"]
                assert sum(v["status"] == "blocked" for v in states.values()) == 32
                assert len(calls) == 3
            finally:
                release.set()
                result = await task
            assert result == {"status": "finished", "remaining": 0, "orders": 35}
            value = (await dispatch.message(db, tenant, parent)).structured_output
            states = value["accounting_group_dispatch"]["members"]
            assert sum(v["status"] == "verified" for v in states.values()) == 2
            assert value["status"] == "indeterminate"
            assert not await dispatch.candidates(db, tenant)
            events = list(await db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant)))
            stopped = next(e for e in events if e.action == "accounting_group.dispatch.stopped")
            assert all(e.timestamp <= stopped.timestamp for e in events if e.action.endswith("dispatch_reserved"))
            # Even successful read-only verification later cannot resume the old
            # group approval. This remains true after worker redelivery/restart.
            child = await dispatch.message(db, tenant, UUID(first))
            child.structured_output = {
                **child.structured_output,
                "status": "approved",
                "accounting_verification": {"status": "verified"},
            }
            await db.commit()
        async with factory() as db:
            assert (await dispatch.run_slice(db, tenant, parent))["status"] == "not_pending"
        assert len(calls) == 3


async def test_recovery_of_existing_unconfirmed_result_audits_stop_once(monkeypatch):
    async with seeded_group(4) as (factory, tenant, parent, so):
        first = so["accounting_group"]["members"][0]["confirmation_id"]
        async with factory() as db:
            message = await dispatch.message(db, tenant, parent)
            value = deepcopy(message.structured_output)
            value["accounting_group_dispatch"]["members"][first] = {"status": "verification_pending"}
            message.structured_output = value
            await db.commit()
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            assert (await dispatch.run_slice(db, tenant, parent))["status"] == "finished"
            assert (await dispatch.run_slice(db, tenant, parent))["status"] == "not_pending"
            events = list(await db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant)))
            stops = [e for e in events if e.action == "accounting_group.dispatch.stopped"]
            assert len(stops) == 1 and stops[0].payload["confirmation_id"] == first
            completed = next(e for e in events if e.action == "accounting_group.completed")
            assert completed.payload["blocked"] == 3 and completed.payload["stopped_after"] == first
        assert not calls
