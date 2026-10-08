from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.transaction_ops.collection_recovery import collection_stop, failure_diagnostic
from app.services.transaction_ops.continuation import next_metadata

NOW = datetime(2026, 10, 7, 20, tzinfo=timezone.utc)


def stopped(exc=ValueError("SQL secret token"), **changes):
    run = SimpleNamespace(
        id=uuid4(),
        origin="schedule",
        status="finished",
        termination_reason="error",
        created_at=NOW - timedelta(minutes=20),
        finished_at=NOW,
        params_json={},
        config_snapshot={"mapping_json": {"action_mode": "propose_actions"}},
        progress_json={"processed": 1407, "pending_refs": ["R100120032"], "last_source_id": 16248174},
    )
    run.progress_json["last_collection_failure"] = failure_diagnostic(exc, run_id=run.id, now=NOW, stage="source_page")
    for key, value in changes.items():
        setattr(run, key, value)
    return run


def test_unknown_failure_gets_one_delayed_checkpoint_retry_not_unlimited_productivity():
    run = stopped()
    assert collection_stop(run)
    with pytest.raises(ValueError, match="read_retry_wait"):
        next_metadata(run, NOW)
    metadata = next_metadata(run, NOW + timedelta(minutes=5))
    assert metadata["collection_diagnostic_retry_count"] == 1
    assert metadata["continuation_read_retry_count"] == 1
    run.progress_json.update(metadata, processed=1500)
    with pytest.raises(ValueError, match="collection_diagnostic_retry_limit"):
        next_metadata(run, NOW + timedelta(minutes=25))


@pytest.mark.parametrize("origin", ["manual", "chat", "recovery"])
def test_error_recovery_excludes_financial_execution_and_manual_runs(origin):
    assert not collection_stop(stopped(origin=origin))


def test_failure_diagnostic_never_serializes_exception_message():
    diag = failure_diagnostic(
        ValueError("select token from private_table"), run_id=uuid4(), now=NOW, stage="SQL secret"
    )
    assert "private_table" not in str(diag) and "SQL secret" not in str(diag)
    assert diag["stage"] == "collection"
    assert diag["category"] == "ValueError"


def test_inherited_failure_does_not_authorize_later_run():
    run = stopped()
    run.id = uuid4()
    assert not collection_stop(run)


def test_transient_failure_uses_existing_finite_backoff_and_caps():
    run = stopped(TimeoutError("private body"))
    for count, minutes in [(0, 5), (1, 15), (2, 60)]:
        run.progress_json["continuation_read_retry_count"] = count
        assert next_metadata(run, NOW + timedelta(minutes=minutes))["continuation_read_retry_count"] == count + 1
    run.progress_json["continuation_read_retry_count"] = 3
    with pytest.raises(ValueError, match="read_retry_limit"):
        next_metadata(run, NOW + timedelta(hours=2))
    run.progress_json["continuation_read_retry_count"] = 0
    with pytest.raises(ValueError, match="cycle_expired"):
        next_metadata(run, NOW + timedelta(days=1))


def test_permanent_database_error_is_diagnosed_without_automatic_retry():
    from sqlalchemy.exc import IntegrityError

    run = stopped(IntegrityError("private SQL", {}, ValueError("secret")))
    assert run.progress_json["last_collection_failure"]["code"] == "collection_permanent"
    assert collection_stop(run)
    with pytest.raises(ValueError, match="collection_failure_permanent"):
        next_metadata(run, NOW + timedelta(minutes=5))


def test_status_and_freshness_agree_on_backoff_and_exhaustion():
    from app.services.transaction_ops import operational_status as status
    from tests.test_transaction_freshness import check, entity
    from tests.test_transaction_freshness import stopped as stale

    run = stopped()
    view = status.continuation_status(run, NOW)
    assert view["state"] == "waiting_for_retry"
    e = stale(entity())
    e["continuation"] = view
    assert check(e)["state"] == "within_grace"
    run.progress_json["collection_diagnostic_retry_count"] = 1
    e["continuation"] = status.continuation_status(run, NOW + timedelta(minutes=5))
    assert e["continuation"]["reason"] == "collection_diagnostic_retry_limit"
    assert check(e)["state"] == "alert"


async def test_error_continuation_retains_checkpoint_and_deduplicates_deliveries(db, admin_user):
    from app.services.transaction_ops import continuation
    from tests.test_transaction_continuation import budget_run

    actor = admin_user[0]
    prior, _ = await budget_run(
        db, actor, origin="schedule", reason="error", failure=TimeoutError(), progress={"last_source_id": 16248174}
    )
    now = prior.finished_at + timedelta(minutes=5)
    saved = dict(prior.progress_json)
    assert await continuation.continue_budget_run(db, actor.tenant_id, prior.id, now=prior.finished_at) is None
    child = await continuation.continue_budget_run(db, actor.tenant_id, prior.id, now=now)
    assert child.progress_json["pending_refs"] == saved["pending_refs"]
    assert child.progress_json["last_source_id"] == 16248174
    assert child.progress_json["continuation_read_retry_count"] == 1
    assert (await continuation.continue_budget_run(db, actor.tenant_id, prior.id, now=now)).id == child.id
    assert prior.progress_json == saved  # Finished evidence is never rewritten.


async def test_legacy_error_requires_operator_permission_and_consumes_unknown_allowance(db, admin_user):
    from app.services.transaction_ops import continuation, state_service
    from tests.test_transaction_continuation import budget_run

    actor = admin_user[0]
    prior, _ = await budget_run(db, actor, origin="schedule", reason="error")
    now = prior.finished_at + timedelta(minutes=5)
    assert await continuation.continue_budget_run(db, actor.tenant_id, prior.id, now=now) is None
    with pytest.raises(state_service.StateError, match="human_actor_required"):
        await continuation.continue_budget_run(db, actor.tenant_id, prior.id, now=now, operator_retry=True)
    child = await continuation.continue_budget_run(
        db, actor.tenant_id, prior.id, now=now, operator_retry=True, actor=actor
    )
    assert child.progress_json["collection_diagnostic_retry_count"] == 1
    assert child.initiated_by == actor.id and child.progress_json["continuation_of"] == str(prior.id)
    from sqlalchemy import select

    from app.models.audit import AuditEvent

    receipt = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(child.id),
            AuditEvent.action == "transaction_ops.run.collection_retry_authorized",
        )
    )
    assert receipt.payload["parent_run_id"] == str(prior.id)


async def test_failure_finish_records_committed_cursor_and_fences_owner(db, admin_user):
    from app.schemas.transaction_runs import ProgressUpdate, RunCreate
    from app.services.transaction_ops import state_service as state
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    conf = await seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db,
        actor.tenant_id,
        conf.id,
        RunCreate(evaluation_key="atomic-failure", order_references=["R100120032"]),
        actor=actor,
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.update_progress(
        db,
        actor.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"processed": 7, "last_source_id": 100, "pending_refs": ["R100120032"]}),
        lease_token=token,
    )
    now = datetime.now(timezone.utc)
    diagnostic = failure_diagnostic(ValueError("private body SQL"), run_id=run.id, now=now, stage="record_finding")
    diagnostic["cursor"] = {"processed": 999, "last_source_id": 999}  # Never trust uncommitted state.
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.finish_run(db, actor.tenant_id, run.id, "error", lease_token=uuid4(), failure=diagnostic)
    row = await state.finish_run(db, actor.tenant_id, run.id, "error", lease_token=token, failure=diagnostic)
    assert row.progress_json["last_collection_failure"]["cursor"] == {"processed": 7, "last_source_id": 100}
    assert row.progress_json["pending_refs"] == ["R100120032"]
    assert "private" not in str(row.progress_json)


async def test_runner_persists_stage_before_failed_transaction_is_discarded():
    from unittest.mock import AsyncMock

    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_ops_runner import NOW as RUN_NOW
    from tests.test_transaction_ops_runner import State

    state = State()
    captured = {}
    original = state.finish_run

    async def finish(*args, **kwargs):
        captured.update(kwargs["failure"])
        return await original(*args, **kwargs)

    state.finish_run = finish
    await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=AsyncMock(side_effect=ValueError("secret body")),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: RUN_NOW,
    )
    assert captured["stage"] == "source_order" and captured["category"] == "ValueError"
    assert "secret" not in str(captured)


def test_auth_failure_keeps_existing_credential_gated_recovery():
    from app.services.transaction_ops import continuation
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

    run = stopped(NetSuiteEvidenceError("upstream_http_401"))
    run.progress_json["last_read_failure"] = {
        "code": "netsuite_upstream_http_401",
        "run_id": str(run.id),
        "resolved": False,
        "observed_at": NOW.isoformat(),
    }
    assert not collection_stop(run)
    assert continuation.next_metadata(run, NOW)["auth_resume_count"] == 1


async def test_collection_retry_rejects_changed_account_scope(db, admin_user, monkeypatch):
    from app.services.transaction_ops import continuation
    from tests.test_transaction_continuation import budget_run

    actor = admin_user[0]
    prior, conf = await budget_run(db, actor, origin="schedule", reason="error", failure=TimeoutError())
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import state_service

    changed = SimpleNamespace(**{key: value for key, value in conf.__dict__.items() if not key.startswith("_")})
    changed.netsuite_account_id = "different-account"
    monkeypatch.setattr(state_service, "get_config", AsyncMock(return_value=changed))
    assert (
        await continuation.continue_budget_run(
            db, actor.tenant_id, prior.id, now=prior.finished_at + timedelta(minutes=5)
        )
        is None
    )
    assert (await continuation.continuation_result(db, actor.tenant_id, prior.id))[1][
        "reason"
    ] == "collection_scope_changed"
