"""Real database + authenticated API tests for isolated historical receipts."""

from copy import deepcopy
from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.transaction_ops import router
from app.models.transaction_ops import TransactionCase, TransactionConfig, TransactionFinding, TransactionRun
from app.models.transaction_policy_replay import TransactionPolicyReplay, TransactionPolicyReplayEntry
from app.services.transaction_ops import policy_replay as service
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_policy_equivalence import NOW, link, saved_report, snapshots
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture(autouse=True)
async def routes(app, monkeypatch):
    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(service, "utc_now", lambda: NOW)
    monkeypatch.setattr(service, "publish", AsyncMock(return_value=True))


async def seed(db, actor):
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    before, _ = snapshots()
    source = await seed_config(
        db,
        actor.tenant_id,
        actor,
        netsuite_account_id="123-sb1",
        mapping_json={
            "reference_field": "tranid",
            "currency_minor_units": {"USD": 2},
            "action_mode": "detect_only",
            "refund_adjustments": before["mapping_json"]["refund_adjustments"],
        },
    )
    values = {
        col.name: deepcopy(getattr(source, col.name))
        for col in TransactionConfig.__table__.columns
        if col.name not in {"id", "created_at", "updated_at"}
    }
    values.update(config_key=uuid4().hex, supersedes_config_id=source.id)
    values["mapping_json"]["refund_adjustments"]["tax_reversal_reason_ids"].append("4")
    target = TransactionConfig(id=uuid4(), **values)
    db.add(target)
    run = TransactionRun(
        id=uuid4(),
        tenant_id=actor.tenant_id,
        config_id=source.id,
        work_key=uuid4().hex,
        origin="schedule",
        params_json={
            "window_start": "2026-09-01T07:00:00+00:00",
            "window_end": "2026-09-02T07:00:00+00:00",
            "window_basis": "updated_at",
            "order_references": [],
        },
        config_snapshot=state._config_snapshot(source),
        status="finished",
        termination_reason="done",
        max_api_calls=100,
        max_orders=100,
        api_calls_used=0,
        orders_used=0,
        deadline_at=NOW,
        finished_at=NOW,
        progress_json={"scan_complete": True, "refund_scan_complete": True, "destination_scan_complete": True},
    )
    db.add(run)
    await db.flush()
    for number, reason in enumerate((None, "4", "missing")):
        report = saved_report()
        report["order_reference"] = f"R12345678{number}"
        for side in report["refund_evidence"].values():
            side["order_reference"] = report["order_reference"]
        report["refund_evidence"]["target"]["connection_id"] = str(source.netsuite_connection_id)
        if reason == "4":
            report["refund_evidence"]["target"]["request_links"] = [link("4")]
        elif reason == "missing":
            report["refund_evidence"]["target"].pop("request_links")
        db.add(
            TransactionFinding(
                tenant_id=actor.tenant_id, run_id=run.id, order_reference=report["order_reference"], report_json=report
            )
        )
    await db.flush()
    return source, target, run


def request():
    return service.ReplayRequest(evaluation_key=uuid4(), start_date=date(2026, 9, 1), end_date=date(2026, 9, 1))


async def test_api_pins_includes_matches_replays_and_preserves_daily_cases(client, db, admin_user):
    actor, headers = admin_user
    source, target, run = await seed(db, actor)
    before = deepcopy(run.progress_json)
    case_count = await db.scalar(
        select(func.count()).select_from(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id)
    )
    body = request().model_dump(mode="json")
    response = await client.post(
        f"/api/v1/transaction-ops/configs/{target.id}/policy-replays", json=body, headers=headers
    )
    assert response.status_code == 202, response.text
    replay_id = response.json()["id"]
    assert response.json()["manifest"]["candidate_count"] == 3
    assert response.json()["manifest"]["original_scan_coverage_complete"] is True
    assert response.json()["manifest"]["population_completeness"] == "unverified"
    assert await service.process_batch(db, actor.tenant_id, replay_id) == "finished"
    assert await service.process_batch(db, actor.tenant_id, replay_id) == "finished"
    summary = await client.get(f"/api/v1/transaction-ops/policy-replays/{replay_id}", headers=headers)
    assert summary.json()["counts"] == {"equivalent": 1, "affected": 1, "unknown": 1}
    retry = await client.post(f"/api/v1/transaction-ops/configs/{target.id}/policy-replays", json=body, headers=headers)
    assert retry.json()["id"] == replay_id
    assert retry.json()["queued"] is False
    entries = await client.get(
        f"/api/v1/transaction-ops/policy-replays/{replay_id}/entries?outcome=affected", headers=headers
    )
    assert len(entries.json()) == 1
    assert entries.json()[0]["result"]["reason"] == "changed_refund_reason"
    assert entries.json()[0]["original_hash"] and len(entries.json()[0]["original_hash"]) == 64
    await db.refresh(run)
    assert run.progress_json == before
    assert run.api_calls_used == 0
    assert (
        await db.scalar(
            select(func.count()).select_from(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id)
        )
        == case_count
    )


async def test_interrupted_batch_rolls_back_results_and_resume_does_not_duplicate(db, admin_user, monkeypatch):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    tenant_id = actor.tenant_id
    replay = await service.create_replay(db, tenant_id, target.id, request(), actor=actor)
    monkeypatch.setattr(service, "BATCH_SIZE", 1)
    assert await service.process_batch(db, tenant_id, replay.id) == "pending"
    commit = state._commit

    async def fail(*args):
        raise RuntimeError("interrupted before commit")

    monkeypatch.setattr(state, "_commit", fail)
    with pytest.raises(RuntimeError):
        await service.process_batch(db, tenant_id, replay.id)
    replay_id = replay.id
    await db.rollback()
    monkeypatch.setattr(state, "_commit", commit)
    assert (await service.status(db, tenant_id, replay_id))["processed"] == 1
    assert await service.process_batch(db, tenant_id, replay_id) == "pending"
    assert await service.process_batch(db, tenant_id, replay_id) == "finished"
    assert (await service.status(db, tenant_id, replay_id))["counts"] == {
        "equivalent": 1,
        "affected": 1,
        "unknown": 1,
    }


async def test_changed_finding_refuses_reuse_and_new_findings_do_not_expand_manifest(db, admin_user):
    actor, _ = admin_user
    _, target, run = await seed(db, actor)
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    finding = await db.scalar(
        select(TransactionFinding)
        .where(TransactionFinding.run_id == run.id)
        .order_by(TransactionFinding.order_reference)
    )
    finding.report_json = {**finding.report_json, "changed": True}
    db.add(
        TransactionFinding(
            tenant_id=actor.tenant_id, run_id=run.id, order_reference="R999999999", report_json=saved_report()
        )
    )
    await db.flush()
    await service.process_batch(db, actor.tenant_id, replay.id)
    rows = await service.entries(db, actor.tenant_id, replay.id)
    assert len(rows) == 3
    assert rows[0]["result"]["reason"] == "original_evidence_changed"


async def test_tenant_authorization_and_cancel(client, db, admin_user, admin_user_b):
    actor, headers = admin_user
    other, other_headers = admin_user_b
    _, target, _ = await seed(db, actor)
    await enable_feature_flag(db, other.tenant_id, "celigo")
    await enable_feature_flag(db, other.tenant_id, "reconciliation")
    response = await client.post(
        f"/api/v1/transaction-ops/configs/{target.id}/policy-replays",
        json=request().model_dump(mode="json"),
        headers=other_headers,
    )
    assert response.status_code == 404
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    url = f"/api/v1/transaction-ops/policy-replays/{replay.id}"
    for suffix in ("", "/entries"):
        assert (await client.get(url + suffix, headers=other_headers)).status_code == 404
    assert (await client.post(url + "/resume", headers=other_headers)).status_code == 404
    assert (await client.post(url + "/cancel", headers=other_headers)).status_code == 404
    assert (await client.post(url + "/cancel", headers=headers)).json()["status"] == "cancelled"
    assert await service.process_batch(db, actor.tenant_id, replay.id) == "cancelled"
    assert (await client.post(url + "/resume", headers=headers)).json()["queued"] is False


async def test_idempotency_key_rejects_other_dates(db, admin_user):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    body = request()
    await service.create_replay(db, actor.tenant_id, target.id, body, actor=actor)
    with pytest.raises(state.StateError, match="evaluation_key_conflict"):
        await service.create_replay(
            db, actor.tenant_id, target.id, body.model_copy(update={"end_date": date(2026, 9, 2)}), actor=actor
        )


async def test_receipt_database_immutability_and_real_non_bypass_rls(db, admin_user, tenant_b):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    await service.process_batch(db, actor.tenant_id, replay.id)
    async with db.begin_nested():
        with pytest.raises(DBAPIError, match="immutable policy replay entry"):
            async with db.begin_nested():
                await db.execute(
                    text("UPDATE transaction_policy_replay_entries SET result_json='{}' WHERE replay_id=:id"),
                    {"id": replay.id},
                )
    role = f"replay_rls_{uuid4().hex[:12]}"
    await db.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    await db.execute(
        text(f"GRANT SELECT, INSERT ON transaction_policy_replays,transaction_policy_replay_entries TO {role}")
    )
    await db.execute(text(f"SET LOCAL ROLE {role}"))
    try:
        assert await db.scalar(select(func.count()).select_from(TransactionPolicyReplay)) == 1
        assert await db.scalar(select(func.count()).select_from(TransactionPolicyReplayEntry)) == 3
        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO transaction_policy_replay_entries "
                        "(tenant_id,replay_id,order_reference,run_id,finding_id,original_hash) "
                        "SELECT :other,replay_id,'R000000000',run_id,finding_id,original_hash "
                        "FROM transaction_policy_replay_entries LIMIT 1"
                    ),
                    {"other": tenant_b.id},
                )
        await db.execute(
            text("SELECT set_config('app.current_tenant_id', :tenant, true)"), {"tenant": str(tenant_b.id)}
        )
        assert await db.scalar(select(func.count()).select_from(TransactionPolicyReplay)) == 0
        assert await db.scalar(select(func.count()).select_from(TransactionPolicyReplayEntry)) == 0
    finally:
        await db.execute(text("RESET ROLE"))


async def test_pending_job_prevents_duplicate_population_and_broker_failure_is_visible(
    client, db, admin_user, monkeypatch
):
    actor, headers = admin_user
    _, target, _ = await seed(db, actor)
    monkeypatch.setattr(service, "publish", AsyncMock(return_value=False))
    url = f"/api/v1/transaction-ops/configs/{target.id}/policy-replays"
    body = request().model_dump(mode="json")
    first = await client.post(url, json=body, headers=headers)
    assert first.status_code == 202 and first.json()["queued"] is False
    duplicate = await client.post(url, json=request().model_dump(mode="json"), headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "policy_replay_already_pending"
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == first.json()["id"]


async def test_partial_historical_windows_cannot_become_complete_period(db, admin_user):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    body = request().model_copy(update={"end_date": date(2026, 9, 2)})
    replay = await service.create_replay(db, actor.tenant_id, target.id, body, actor=actor)
    await service.process_batch(db, actor.tenant_id, replay.id)
    result = await service.status(db, actor.tenant_id, replay.id)
    assert result["status"] == "finished"
    assert result["manifest"]["original_scan_coverage_complete"] is False
    assert result["manifest"]["population_completeness"] == "unverified"
    assert result["manifest"]["financial_certification"] == "not_certified"
    assert result["manifest"]["daily_coverage_advanced"] is False


async def test_worker_failure_is_visible_and_successful_resume_clears_it(db, admin_user):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    await service.record_failure(db, actor.tenant_id, replay.id)
    assert (await service.status(db, actor.tenant_id, replay.id))["last_error_code"] == "policy_replay_failed"
    await service.process_batch(db, actor.tenant_id, replay.id)
    assert (await service.status(db, actor.tenant_id, replay.id))["last_error_code"] is None


async def test_revoked_initiator_permission_blocks_worker_execution(db, admin_user):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    actor.is_active = False
    await db.flush()
    with pytest.raises(state.StateError, match="human_actor_required"):
        await service.process_batch(db, actor.tenant_id, replay.id)


async def test_changed_evaluator_contract_has_explicit_error(db, admin_user, monkeypatch):
    actor, _ = admin_user
    _, target, _ = await seed(db, actor)
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    monkeypatch.setattr(service.policy_equivalence, "VERSION", 2)
    with pytest.raises(state.StateError, match="policy_replay_contract_changed"):
        await service.process_batch(db, actor.tenant_id, replay.id)


@pytest.mark.parametrize("code", ["57014", "55P03"])
async def test_database_timeouts_are_coded_without_query_details(code):
    class TimeoutError(Exception):
        sqlstate = code

    @service.database_errors
    async def fails():
        raise DBAPIError("secret query", {}, TimeoutError("secret"))

    with pytest.raises(state.StateError) as error:
        await fails()
    assert error.value.code == "policy_replay_database_busy"
    assert error.value.http_status == 503


async def test_unknown_window_basis_is_not_assumed_to_be_updated_at(db, admin_user):
    actor, _ = admin_user
    source, target, run = await seed(db, actor)
    ambiguous = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=source.id,
        work_key=uuid4().hex,
        origin="schedule",
        params_json={key: value for key, value in run.params_json.items() if key != "window_basis"},
        config_snapshot=run.config_snapshot,
        status="finished",
        termination_reason="done",
        max_api_calls=100,
        max_orders=100,
        api_calls_used=0,
        orders_used=0,
        deadline_at=NOW,
        finished_at=NOW,
        progress_json=run.progress_json,
    )
    db.add(ambiguous)
    await db.flush()
    db.add(
        TransactionFinding(
            tenant_id=actor.tenant_id, run_id=ambiguous.id, order_reference="R000000000", report_json=saved_report()
        )
    )
    await db.flush()
    replay = await service.create_replay(db, actor.tenant_id, target.id, request(), actor=actor)
    assert replay.manifest_json["candidate_count"] == 3


async def test_ambiguous_basis_scan_cannot_fill_a_coverage_gap(db, admin_user):
    actor, _ = admin_user
    source, target, run = await seed(db, actor)
    ambiguous = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=source.id,
        work_key=uuid4().hex,
        origin="schedule",
        params_json={
            "window_start": "2026-09-02T07:00:00+00:00",
            "window_end": "2026-09-03T07:00:00+00:00",
            "order_references": [],
        },
        config_snapshot=run.config_snapshot,
        status="finished",
        termination_reason="done",
        max_api_calls=100,
        max_orders=100,
        api_calls_used=0,
        orders_used=0,
        deadline_at=NOW,
        finished_at=NOW,
        progress_json=run.progress_json,
    )
    db.add(ambiguous)
    await db.flush()
    body = request().model_copy(update={"end_date": date(2026, 9, 2)})
    replay = await service.create_replay(db, actor.tenant_id, target.id, body, actor=actor)
    assert replay.manifest_json["original_scan_coverage_complete"] is False
    assert str(ambiguous.id) not in replay.manifest_json["original_scan_run_ids"]
