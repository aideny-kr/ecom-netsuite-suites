"""Spend persists before reads and crash recovery fences the one send permit."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_dispatch as dispatch_fixtures

ready = dispatch_fixtures.ready
reserve = dispatch_fixtures.reserve


async def operation(db, claim):
    return (
        await db.execute(
            select(TransactionOperation)
            .where(TransactionOperation.id == claim.operation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_claim_has_fixed_budget_before_any_provider_call(db, ready):
    actor, _, proposal, claim = ready
    row = await operation(db, claim)
    assert row.max_api_calls == 96
    assert row.api_calls_used == 0
    assert row.attempted_at < row.deadline_at <= min(proposal.valid_until, row.attempted_at + timedelta(seconds=300))
    permit = await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=12)
    assert permit.deadline_at == row.deadline_at
    assert permit.remaining_api_calls == 84
    assert (await operation(db, claim)).api_calls_used == 12
    # A new claim must not reset the original operation's budget.
    with pytest.raises(state.StateError, match="operation_already_attempted"):
        await state.claim_approved_operation(
            db, actor.tenant_id, proposal.id, expected_evidence_fingerprint=proposal.evidence_fingerprint
        )


@pytest.mark.parametrize("cost", [0, -1, True, 1.5, 97])
async def test_invalid_read_cost_cannot_spend(db, ready, cost):
    actor, _, _, claim = ready
    with pytest.raises(ValueError):
        await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=cost)
    assert (await operation(db, claim)).api_calls_used == 0


async def test_no_dispatch_after_read_budget_exhaustion(db, ready):
    actor, _, _, claim = ready
    assert await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=96)
    assert await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=1) is None
    row = await operation(db, claim)
    assert row.status == "failed"
    assert row.result_json["termination_reason"] == "budget"
    assert row.result_json["code"] == "operation_budget_exhausted"
    with pytest.raises(state.StateError):
        await reserve(db, actor.tenant_id, claim)


async def test_dispatch_itself_consumes_budget_and_preserves_read_spend(db, ready):
    actor, _, _, claim = ready
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=10)
    assert await reserve(db, actor.tenant_id, claim)
    assert (await operation(db, claim)).api_calls_used == 11
    assert not await reserve(db, actor.tenant_id, claim)
    assert (await operation(db, claim)).api_calls_used == 11
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="unknown", result_json={"code": "provider_timeout"}
    )
    row = await operation(db, claim)
    assert row.result_json["dispatch_reserved"] is True
    assert row.result_json["payload_fingerprint"] == "b" * 64
    assert row.result_json["termination_reason"] == "stall"


async def test_dispatch_requires_an_available_call_and_time(db, ready):
    actor, _, _, claim = ready
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=96)
    with pytest.raises(state.StateError, match="operation_budget_exhausted"):
        await reserve(db, actor.tenant_id, claim)
    row = await operation(db, claim)
    assert row.status == "failed"
    assert row.result_json.get("dispatch_reserved") is not True


@pytest.mark.parametrize("sent", [False, True])
async def test_crash_recovery_fences_old_worker_before_or_after_send(db, ready, sent):
    actor, _, _, claim = ready
    if sent:
        assert await reserve(db, actor.tenant_id, claim)
    row = await operation(db, claim)
    assert (
        await state.recover_expired_operation(
            db, actor.tenant_id, row.id, now=row.deadline_at - timedelta(microseconds=1)
        )
        is None
    )
    recovered = await state.recover_expired_operation(db, actor.tenant_id, row.id, now=row.deadline_at)
    assert recovered.status == ("unknown" if sent else "failed")
    assert recovered.result_json["termination_reason"] == "budget"
    assert recovered.result_json["code"] == ("interrupted_after_dispatch" if sent else "interrupted_before_dispatch")
    if sent:
        assert not await reserve(db, actor.tenant_id, claim)
    else:
        with pytest.raises(state.StateError, match="operation_not_executable"):
            await reserve(db, actor.tenant_id, claim)
    assert await state.recover_expired_operation(db, actor.tenant_id, row.id, now=row.deadline_at) is None


async def test_read_deadline_after_send_is_unknown_never_failed(db, ready):
    actor, _, _, claim = ready
    await reserve(db, actor.tenant_id, claim)
    row = await operation(db, claim)
    assert await state.reserve_operation_budget(db, actor.tenant_id, row.id, api_calls=1, now=row.deadline_at) is None
    assert (await operation(db, claim)).status == "unknown"


@pytest.mark.parametrize(
    "key,value",
    [
        ("dispatch_reserved", False),
        ("payload_fingerprint", "c" * 64),
        ("provider", "celigo"),
        ("dispatch_reserved_at", "2020-01-01T00:00:00Z"),
        ("termination_reason", "done"),
    ],
)
async def test_provider_result_cannot_overwrite_ledger_facts(db, ready, key, value):
    actor, _, _, claim = ready
    await reserve(db, actor.tenant_id, claim)
    with pytest.raises(state.StateError, match="reserved_operation_result_key"):
        await state.complete_operation(
            db, actor.tenant_id, claim.operation_id, outcome="unknown", result_json={key: value}
        )
    assert (await operation(db, claim)).result_json["dispatch_reserved"] is True


async def test_foreign_tenant_cannot_spend_or_recover(db, ready, admin_user_b):
    _, _, _, claim = ready
    other, _ = admin_user_b
    with pytest.raises(state.StateError):
        await state.reserve_operation_budget(db, other.tenant_id, claim.operation_id, api_calls=1)
    with pytest.raises(state.StateError):
        await state.recover_expired_operation(
            db, other.tenant_id, claim.operation_id, now=datetime.now(timezone.utc) + timedelta(hours=1)
        )


async def test_revoked_feature_stops_reads_before_spend(db, ready):
    actor, _, _, claim = ready
    await db.execute(
        text("UPDATE tenant_feature_flags SET enabled=false WHERE tenant_id=:tenant AND flag_key='celigo'"),
        {"tenant": actor.tenant_id},
    )
    await db.flush()
    with pytest.raises(state.StateError, match="feature_disabled"):
        await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=2)
    assert (await operation(db, claim)).api_calls_used == 0


async def test_spend_and_dispatch_facts_cannot_be_reset_in_sql(db, ready):
    actor, _, _, claim = ready
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=4)
    await reserve(db, actor.tenant_id, claim)
    for change in (
        "max_api_calls=95",
        "api_calls_used=0",
        "deadline_at=deadline_at + interval '1 second'",
        "result_json='{}'::jsonb",
        'result_json=result_json || \'{"provider":"celigo"}\'::jsonb',
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with db.begin_nested():
                await db.execute(
                    text(f"UPDATE transaction_ops_operations SET {change} WHERE id=:id"), {"id": claim.operation_id}
                )


async def test_budget_reservation_commits_before_return_and_restores_tenant(db, ready, monkeypatch):
    actor, _, _, claim = ready
    events = []
    original_commit, original_context = db.commit, state.set_tenant_context

    async def commit():
        row = await operation(db, claim)
        assert row.api_calls_used == 2
        events.append("commit_spend")
        await original_commit()

    async def context(session, tenant):
        events.append("tenant_context")
        await original_context(session, tenant)

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(state, "set_tenant_context", context)
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=2)
    assert events[-2:] == ["commit_spend", "tenant_context"]
