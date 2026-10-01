from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, update

from app.models.feature_flag import TenantFeatureFlag
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import continuation, framework_defaults, state_service
from tests.test_transaction_defaults import connections


@pytest.fixture(autouse=True)
def distinct_run_creation_times():
    # The test DB wraps commits in savepoints, so PostgreSQL now() otherwise
    # gives every run the same outer-transaction timestamp. Production commits
    # separate these inserts; preserve that ordering without editing immutable
    # created_at values after insertion or depending on random evaluation keys.
    def set_created_at(mapper, connection, target):
        if target.created_at is None:
            target.created_at = datetime.now(timezone.utc)

    event.listen(TransactionRun, "before_insert", set_created_at)
    try:
        yield
    finally:
        event.remove(TransactionRun, "before_insert", set_created_at)


async def budget_run(db, user, *, reason="budget", progress=None, origin="manual"):
    await db.execute(
        update(TenantFeatureFlag)
        .where(
            TenantFeatureFlag.tenant_id == user.tenant_id, TenantFeatureFlag.flag_key.in_(("celigo", "reconciliation"))
        )
        .values(enabled=True)
    )
    source, _ = await connections(db, user.tenant_id)
    config = (await framework_defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user))[0]
    if origin == "schedule":
        config.schedule_enabled = True
        await db.flush()
    run = await state_service.create_run(
        db,
        user.tenant_id,
        config.id,
        RunCreate(origin=origin, evaluation_key=str(uuid4()), order_references=("R100120031", "R100120032")),
        actor=user,
    )
    run.status, run.termination_reason = "finished", reason
    run.finished_at = datetime.now(timezone.utc)
    run.progress_json = {
        "processed": 1,
        "scan_count": 0,
        "pending_refs": ["R100120032"],
        "scan_complete": True,
        **(progress or {}),
    }
    if run.progress_json.get("read_stop_reason"):
        run.progress_json.setdefault("read_stop_run_id", str(run.id))
    await db.flush()
    return run, config


async def test_budget_continuation_keeps_scope_cursor_and_human_provenance(db, admin_user):
    user, _ = admin_user
    prior, _ = await budget_run(db, user)
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    assert child.id != prior.id and child.initiated_by == user.id and child.origin == "manual"
    assert child.params_json["order_references"] == prior.params_json["order_references"]
    assert child.progress_json["pending_refs"] == ["R100120032"]
    assert child.progress_json["continuation_part"] == 2
    assert child.progress_json["continuation_baseline"] == {"processed": 1, "scan_count": 0}
    assert (await continuation.continuation_result(db, user.tenant_id, prior.id))[0].id == child.id
    again = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    assert again.id == child.id
    assert "continuation_run_id" not in prior.progress_json  # Terminal evidence remains immutable.


async def test_retry_exhaustion_preserves_finite_cycle_and_cannot_loop_without_progress(db, admin_user):
    user = admin_user[0]
    prior, _ = await budget_run(
        db,
        user,
        progress={
            "read_retry_count": 3,
            "read_stop_reason": "retry_limit",
            "last_read_failure": {"code": "netsuite_read_transport_failed", "retryable": True, "resolved": False},
        },
    )
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    assert child.progress_json["read_retry_count"] == 3
    assert child.progress_json["pending_refs"] == prior.progress_json["pending_refs"]
    assert child.progress_json["continuation_root_id"] == str(prior.id)
    child.status, child.termination_reason = "finished", "budget"
    child.finished_at = datetime.now(timezone.utc)
    await db.flush()
    assert await continuation.continue_budget_run(db, user.tenant_id, child.id) is None
    assert (await continuation.continuation_result(db, user.tenant_id, child.id))[1]["reason"] == "no_progress"


@pytest.mark.parametrize("reason", ["error", "stall", "done"])
async def test_failures_and_complete_runs_do_not_reset_their_budget(db, admin_user, reason):
    user, _ = admin_user
    run, _ = await budget_run(db, user, reason=reason)
    assert await continuation.continue_budget_run(db, user.tenant_id, run.id) is None


@pytest.mark.parametrize("guard", ["no_progress", "part_limit", "paused"])
async def test_automatic_continuations_have_finite_limits_and_require_progress(db, admin_user, guard):
    user, _ = admin_user
    progress = {}
    if guard == "no_progress":
        progress = {"continuation_baseline": {"processed": 1, "scan_count": 0}}
    elif guard == "part_limit":
        progress = {"continuation_part": continuation.MAX_PARTS}
    run, config = await budget_run(db, user, progress=progress)
    if guard == "paused":
        config.enabled = config.schedule_enabled = False
    await db.flush()
    assert await continuation.continue_budget_run(db, user.tenant_id, run.id) is None
    assert (await continuation.continuation_result(db, user.tenant_id, run.id))[1]["reason"]


async def test_revoked_feature_stops_continuation_and_other_tenants_cannot_resume(db, admin_user, admin_user_b):
    user, _ = admin_user
    run, _ = await budget_run(db, user)
    with pytest.raises(state_service.StateError, match="not_found"):
        await continuation.continue_budget_run(db, admin_user_b[0].tenant_id, run.id)
    await db.execute(
        update(TenantFeatureFlag)
        .where(TenantFeatureFlag.tenant_id == user.tenant_id, TenantFeatureFlag.flag_key == "reconciliation")
        .values(enabled=False)
    )
    assert await continuation.continue_budget_run(db, user.tenant_id, run.id) is None
    assert (await continuation.continuation_result(db, user.tenant_id, run.id))[1]["reason"] == "feature_unavailable"


async def test_run_api_links_to_its_continuation_without_changing_terminal_evidence(client, db, admin_user):
    user, headers = admin_user
    prior, _ = await budget_run(db, user)
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    response = await client.get(f"/api/v1/transaction-ops/runs/{prior.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["continuation_run_id"] == str(child.id)
    assert "continuation_run_id" not in response.json()["progress_json"]


@pytest.mark.parametrize("counter", ["destination_scan_count", "dependency_step_count"])
def test_destination_cursor_progress_allows_bounded_continuation(counter):
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    prior = SimpleNamespace(
        id=uuid4(),
        created_at=now,
        progress_json={
            "processed": 5,
            "scan_count": 20,
            counter: 40,
            "continuation_baseline": {"processed": 5, "scan_count": 20, counter: 20},
        },
    )
    metadata = continuation.next_metadata(prior, now)
    assert metadata["continuation_baseline"][counter] == 40


async def test_scheduler_recovers_dependency_only_progress_and_retains_no_progress_guard(db, admin_user):
    from app.services.transaction_ops.scheduler import _recovery_ids

    actor = admin_user[0]
    prior, _ = await budget_run(
        db,
        actor,
        progress={
            "processed": 0,
            "scan_count": 0,
            "destination_scan_count": 0,
            "dependency_step_count": 3,
            "pending_refs": [],
            "refund_scan_complete": True,
            "dependency_scan": {"version": 1, "stream_index": 1, "after": None},
        },
    )
    now = datetime.now(timezone.utc)
    assert prior.id in await _recovery_ids(db, actor.tenant_id, now)
    assert prior.id not in await _recovery_ids(db, uuid4(), now)
    child = await continuation.continue_budget_run(db, actor.tenant_id, prior.id)
    assert child.progress_json["dependency_scan"] == prior.progress_json["dependency_scan"]
    assert child.progress_json["continuation_baseline"]["dependency_step_count"] == 3
    assert prior.id not in await _recovery_ids(db, actor.tenant_id, now)
    # A child that did no additional discovery cannot earn another budget.
    child.status, child.termination_reason = "finished", "budget"
    child.finished_at = datetime.now(timezone.utc)
    await db.flush()
    assert await continuation.continue_budget_run(db, actor.tenant_id, child.id) is None
    assert (await continuation.continuation_result(db, actor.tenant_id, child.id))[1]["reason"] == "no_progress"


async def scheduled_failure(db, user, *, progress=None):
    prior, config = await budget_run(
        db,
        user,
        origin="schedule",
        progress={
            "read_retry_count": 3,
            "read_stop_reason": "retry_limit",
            "last_read_failure": {"code": "source_transport_failed", "retryable": True, "resolved": False},
            "continuation_baseline": {"processed": 1, "scan_count": 0},
            "last_source_id": 42,
            **(progress or {}),
        },
    )
    return prior, config


@pytest.mark.parametrize(
    "code", ["source_transport_failed", "replica_query_incomplete", "replica_read_not_fresh", "replica_cached_response"]
)
async def test_scheduled_transient_read_waits_then_resumes_old_blocked_checkpoint_once(db, admin_user, code):
    from app.services.transaction_ops.scheduler import _recovery_ids

    user = admin_user[0]
    prior, _ = await scheduled_failure(
        db, user, progress={"last_read_failure": {"code": code, "retryable": True, "resolved": False}}
    )
    before = dict(prior.progress_json)
    due = prior.finished_at + continuation.READ_RETRY_DELAYS[0]
    assert not continuation.read_retry_due(prior, due - timedelta(seconds=1))
    assert await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due - timedelta(seconds=1)) is None
    assert (await continuation.continuation_result(db, user.tenant_id, prior.id))[1] is None
    assert prior.id not in await _recovery_ids(db, user.tenant_id, due)
    # The existing deployment already recorded this refusal for Inc and BV.
    await state_service._audit(db, user.tenant_id, "run.continuation_blocked", prior, payload={"reason": "no_progress"})
    await db.flush()
    assert continuation.read_retry_due(prior, due)
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due)
    assert child.id != prior.id and child.origin == "schedule"
    assert child.progress_json["pending_refs"] == before["pending_refs"]
    assert child.progress_json["last_source_id"] == 42
    assert child.progress_json["read_retry_count"] == 3
    assert child.progress_json["continuation_read_retry_count"] == 1
    assert child.progress_json["evidence_root_id"] == str(prior.id)
    assert prior.progress_json == before
    again = await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due)
    assert again.id == child.id


async def test_delayed_retries_stop_at_three_without_resetting_existing_caps(db, admin_user):
    user = admin_user[0]
    prior, _ = await scheduled_failure(db, user)
    root = prior.id
    for count, delay in enumerate(continuation.READ_RETRY_DELAYS, 1):
        due = prior.finished_at + delay
        child = await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due)
        assert child.progress_json["continuation_read_retry_count"] == count
        assert child.progress_json["continuation_root_id"] == str(root)
        assert child.progress_json["read_retry_count"] == 3
        child.progress_json = {**child.progress_json, "read_stop_run_id": str(child.id)}
        child.status, child.termination_reason, child.finished_at = "finished", "budget", due
        await db.flush()
        prior = child
    assert not continuation.read_retry_due(prior, due + timedelta(hours=2))
    assert await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due + timedelta(hours=2)) is None
    assert (await continuation.continuation_result(db, user.tenant_id, prior.id))[1]["reason"] == "read_retry_limit"


async def test_productive_continuation_retains_delayed_retry_count(db, admin_user):
    user = admin_user[0]
    prior, _ = await scheduled_failure(db, user)
    due = prior.finished_at + continuation.READ_RETRY_DELAYS[0]
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due)
    child.progress_json = {
        **child.progress_json,
        "processed": 2,
        "read_stop_reason": None,
        "last_read_failure": {**child.progress_json["last_read_failure"], "resolved": True},
    }
    child.status, child.termination_reason, child.finished_at = "finished", "budget", due
    await db.flush()
    productive = await continuation.continue_budget_run(db, user.tenant_id, child.id, now=due)
    assert productive.progress_json["continuation_read_retry_count"] == 1
    assert productive.progress_json["read_retry_count"] == 3


@pytest.mark.parametrize("change", ["permanent", "resolved", "restart", "parts", "age", "bad_count"])
async def test_delayed_retry_never_bypasses_safety_guards(db, admin_user, change):
    user = admin_user[0]
    progress = {"last_read_failure": {"code": "source_transport_failed", "retryable": True, "resolved": False}}
    if change == "permanent":
        progress["last_read_failure"]["code"] = "source_authentication_failed"
    elif change == "resolved":
        progress["last_read_failure"]["resolved"] = True
    elif change == "restart":
        progress["restart_scan"] = True
    elif change == "parts":
        progress["continuation_part"] = continuation.SCHEDULE_MAX_PARTS
    elif change == "age":
        progress["continuation_started_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    else:
        progress["continuation_read_retry_count"] = True
    prior, _ = await scheduled_failure(db, user, progress=progress)
    due = prior.finished_at + timedelta(hours=2)
    assert not continuation.read_retry_due(prior, due)
    assert await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=due) is None


@pytest.mark.parametrize("guard", ["paused", "permission_denied", "feature_unavailable"])
async def test_delayed_retry_does_not_override_other_recorded_blocks(db, admin_user, guard):
    user = admin_user[0]
    prior, _ = await scheduled_failure(db, user)
    await state_service._audit(db, user.tenant_id, "run.continuation_blocked", prior, payload={"reason": "no_progress"})
    await state_service._audit(db, user.tenant_id, "run.continuation_blocked", prior, payload={"reason": guard})
    await db.flush()
    assert (
        await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=prior.finished_at + timedelta(hours=2))
        is None
    )


@pytest.mark.parametrize("diagnostic", ["inherited", "legacy", "unrelated"])
async def test_productive_legacy_stop_remains_recoverable_after_worker_exit(db, admin_user, diagnostic):
    from app.services.transaction_ops.scheduler import _recovery_ids

    user = admin_user[0]
    failure = {
        "code": "source_transport_failed",
        "retryable": True,
        "resolved": False,
        "observed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    if diagnostic == "unrelated":
        failure.update(code="unclassified_read_failure", retryable=False)
    prior, _ = await scheduled_failure(
        db,
        user,
        progress={
            "processed": 271,
            "continuation_baseline": {"processed": 149, "scan_count": 0},
            "continuation_read_retry_count": 2,
            "last_read_failure": failure,
            "read_stop_run_id": str(uuid4()) if diagnostic == "inherited" else None,
        },
    )
    assert not continuation.scheduled_read_stop(prior)
    assert prior.id in await _recovery_ids(db, user.tenant_id, prior.finished_at)
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id, now=prior.finished_at)
    assert child.progress_json["continuation_read_retry_count"] == 2
    assert child.progress_json["last_read_failure"] == failure  # Don't erase unrelated diagnostics.


async def test_legacy_real_stop_still_gets_bounded_retry(db, admin_user):
    user = admin_user[0]
    prior, _ = await scheduled_failure(db, user, progress={"read_stop_run_id": None})
    # The default fixture is terminal; use a detached value to prove timestamp compatibility.
    from types import SimpleNamespace

    legacy = SimpleNamespace(
        **{
            k: getattr(prior, k)
            for k in ("id", "origin", "status", "termination_reason", "created_at", "finished_at", "progress_json")
        }
    )
    legacy.progress_json = {
        **prior.progress_json,
        "last_read_failure": {**prior.progress_json["last_read_failure"], "observed_at": prior.finished_at.isoformat()},
    }
    assert continuation.scheduled_read_stop(legacy)
    assert continuation.read_retry_due(legacy, prior.finished_at + timedelta(minutes=5))
