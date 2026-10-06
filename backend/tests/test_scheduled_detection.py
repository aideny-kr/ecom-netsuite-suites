"""Scheduled detection is evidence, never financial authority (synthetic only)."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation, TransactionProposal
from app.schemas.transaction_runs import ConfigControl, RunCreate
from app.services.transaction_ops import case_service
from app.services.transaction_ops import state_service as state
from tests.test_transaction_cases import REF, report
from tests.test_transaction_ops_state_db import seed_config

NOW = datetime.now(timezone.utc)


def evidence():
    body = report("matched", NOW)
    body["source"].update(
        system="framework",
        account_id="frame.work",
        record_id="1",
        order_reference=REF,
        subsidiary_id="5",
        updated_at=(NOW - timedelta(hours=1)).isoformat(),
        currency="USD",
        status="confirmed",
    )
    body["lookup"].update(
        source_system="framework",
        source_account_id="frame.work",
        source_record_id="1",
        order_reference=REF,
        target_account_id="1234567-sb1",
        target_subsidiary_id="5",
        target_record_type="salesorder",
        observed_at=NOW.isoformat(),
    )
    body["targets"][0].update(
        system="netsuite",
        account_id="1234567-sb1",
        record_id="2",
        order_reference=REF,
        subsidiary_id="5",
        record_type="salesorder",
        updated_at=NOW.isoformat(),
        currency="USD",
    )
    return body


def test_missing_timing_and_matched_are_distinct_without_inventing_sync_policy():
    from app.services.transaction_ops.scheduled_detection import classify

    body = evidence()
    assert classify(body, now=NOW)["outcome"] == "no_discrepancy"
    body["targets"] = []
    body["balance"]["status"] = "missing_in_netsuite"
    assert classify(body, now=NOW)["outcome"] == "observed_missing"
    body["source"]["updated_at"] = (NOW - timedelta(seconds=5)).isoformat()
    body["lookup"]["observed_at"] = (NOW - timedelta(seconds=10)).isoformat()
    result = classify(body, now=NOW)
    assert result["outcome"] == "timing_difference"
    assert result["reason"] == "destination_observed_before_source_version"
    assert result["financial_approval"] is None


@pytest.mark.parametrize("variant", ["partial", "stale", "foreign", "currency", "unknown", "future", "future_target"])
def test_unproven_evidence_cannot_assert_match_or_absence(variant):
    from app.services.transaction_ops.scheduled_detection import classify

    body = evidence()
    if variant == "partial":
        body["lookup"]["complete"] = False
    elif variant == "stale":
        body["lookup"]["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
    elif variant == "foreign":
        body["targets"][0]["account_id"] = "other"
    elif variant == "currency":
        body["targets"][0]["currency"] = "EUR"
    elif variant == "unknown":
        body["source"].pop("updated_at")
    elif variant == "future_target":
        body["targets"][0]["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        body["source"]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert classify(body, now=NOW)["outcome"] == "incomplete_evidence"


async def scheduled(db, actor):
    config = await seed_config(db, actor.tenant_id, actor, netsuite_account_id="1234567_SB1")
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(origin="schedule", evaluation_key="test-1", order_references=[REF]),
        now=NOW,
    )
    return config, run


@pytest.mark.parametrize("stage", ["claim", "reserve", "finding"])
async def test_schedule_rechecks_creator_before_read_and_publication(db, admin_user, stage):
    actor = admin_user[0]
    _, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW) if stage != "claim" else None
    actor.is_active = False
    await db.flush()
    if stage == "claim":
        assert await state.claim_run(db, actor.tenant_id, run.id, now=NOW) is None
    elif stage == "reserve":
        assert not await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=1, now=NOW)
    else:
        with pytest.raises(state.StateError, match="scheduled_detection_access_revoked"):
            await state.record_finding(db, actor.tenant_id, run.id, REF, evidence(), lease_token=token, now=NOW)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert (current.status, current.termination_reason, current.api_calls_used) == ("finished", "stall", 0)
    assert not await case_service.list_cases(db, actor.tenant_id)


async def test_receipt_is_server_owned_deduplicated_and_has_no_financial_action(db, admin_user):
    actor = admin_user[0]
    config, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    body = evidence()
    body["targets"] = []
    body["balance"]["status"] = "missing_in_netsuite"
    body["scheduled_detection"] = {"outcome": "approved", "financial_approval": "forged"}
    for _ in range(2):
        row = await state.record_finding(db, actor.tenant_id, run.id, REF, deepcopy(body), lease_token=token, now=NOW)
    receipt = row.report_json["scheduled_detection"]
    assert receipt["outcome"] == "observed_missing"
    assert receipt["principal_id"] == str(actor.id)
    assert receipt["rules"]["config_key"] == config.config_key
    assert receipt["skill"]["applied"] is False
    assert len(receipt["skill"]["version"]) == 64
    assert receipt["accounting_context"]["policy_applied"] is False
    assert receipt["financial_approval"] is None
    cases = await case_service.list_cases(db, actor.tenant_id)
    assert len(cases) == 1
    assert len(await case_service.list_observations(db, actor.tenant_id, cases[0].id)) == 1
    assert not (await db.scalars(select(TransactionProposal))).all()
    assert not (await db.scalars(select(TransactionOperation))).all()


async def test_timing_gap_does_not_resolve_an_existing_case(db, admin_user):
    actor = admin_user[0]
    _, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    body = evidence()
    body["balance"]["status"] = "difference"
    await state.record_finding(db, actor.tenant_id, run.id, REF, body, lease_token=token, now=NOW)
    body["balance"]["status"] = "matched"
    body["lookup"]["observed_at"] = (NOW - timedelta(seconds=10)).isoformat()
    body["source"]["updated_at"] = (NOW - timedelta(seconds=5)).isoformat()
    row = await state.record_finding(db, actor.tenant_id, run.id, REF, body, lease_token=token, now=NOW)
    assert row.report_json["scheduled_detection"]["outcome"] == "timing_difference"
    assert row.report_json["scheduled_detection"]["observed_balance_status"] == "matched"
    assert row.report_json["balance"]["status"] == "incomplete"
    assert row.report_json["balance"]["amounts"]["order_total"]["delta"] == "0.00"
    assert (await case_service.list_cases(db, actor.tenant_id))[0].status == "open"


async def test_disabled_schedule_stops_midrun_without_spending(db, admin_user):
    actor = admin_user[0]
    config, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=False), actor=actor
    )
    assert not await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=2, now=NOW)
    assert run.api_calls_used == 0
    assert run.termination_reason == "stall"


async def test_context_authorization_revoked_between_checks_does_not_publish(db, admin_user, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import context_provenance

    actor = admin_user[0]
    _, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    monkeypatch.setattr(
        context_provenance,
        "context_manifest",
        AsyncMock(return_value={"status": "unavailable", "reason": "permission_denied"}),
    )
    with pytest.raises(state.StateError, match="scheduled_detection_access_revoked"):
        await state.record_finding(db, actor.tenant_id, run.id, REF, evidence(), lease_token=token, now=NOW)
    assert not await state.list_findings(db, actor.tenant_id, run.id)
    assert not await case_service.list_cases(db, actor.tenant_id)
    assert run.termination_reason == "stall"


async def test_rebound_connection_cannot_publish_old_account_evidence(db, admin_user):
    from app.models.connection import Connection

    actor = admin_user[0]
    config, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    conn = await db.get(Connection, config.netsuite_connection_id)
    conn.metadata_json = {"account_id": "9999999_SB1"}
    await db.flush()
    with pytest.raises(state.StateError, match="scheduled_detection_access_revoked"):
        await state.record_finding(db, actor.tenant_id, run.id, REF, evidence(), lease_token=token, now=NOW)
    assert not await state.list_findings(db, actor.tenant_id, run.id)


def test_older_native_version_does_not_invalidate_matching_financial_evidence():
    from app.services.transaction_ops.scheduled_detection import classify

    body = evidence()
    body["targets"][0]["updated_at"] = (NOW - timedelta(hours=2)).isoformat()
    result = classify(body, now=NOW)
    assert result["outcome"] == "no_discrepancy"
    assert result["reason"] == "order_total_tax_refunds_match"


def test_missing_cancelled_order_requires_lifecycle_review():
    from app.services.transaction_ops.scheduled_detection import classify

    body = evidence()
    body["targets"] = []
    body["source"]["status"] = "cancelled"
    assert classify(body, now=NOW)["outcome"] == "needs_review"


@pytest.mark.parametrize(
    "field,value", [("netsuite_account_id", "9999999"), ("subsidiary_id", "7"), ("record_type", "invoice")]
)
def test_receipt_rejects_out_of_scope_evidence(field, value):
    from app.services.transaction_ops.scheduled_detection import classify

    scope = {"netsuite_account_id": "1234567_SB1", "subsidiary_id": "5", "record_type": "salesorder", field: value}
    assert classify(evidence(), now=NOW, scope=scope)["outcome"] == "incomplete_evidence"


async def test_current_role_removal_blocks_scheduled_publication(db, admin_user):
    from sqlalchemy import delete

    from app.models.user import UserRole

    actor = admin_user[0]
    _, run = await scheduled(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    await db.execute(delete(UserRole).where(UserRole.tenant_id == actor.tenant_id, UserRole.user_id == actor.id))
    with pytest.raises(state.StateError, match="scheduled_detection_access_revoked"):
        await state.record_finding(db, actor.tenant_id, run.id, REF, evidence(), lease_token=token, now=NOW)
    assert not await state.list_findings(db, actor.tenant_id, run.id)


def test_native_version_order_cannot_turn_amount_difference_into_sync_delay():
    from app.services.transaction_ops.scheduled_detection import classify

    body = evidence()
    body["balance"] = report("difference", NOW)["balance"]
    body["targets"][0]["updated_at"] = (NOW - timedelta(hours=2)).isoformat()
    assert classify(body, now=NOW) == {
        "outcome": "needs_review",
        "reason": "observed_amount_difference",
        "financial_approval": None,
    }


@pytest.mark.parametrize(
    "variant", ["approved", "missing", "draft", "invalidated", "revision", "hash", "currency", "scope", "expired"]
)
async def test_selected_context_is_pinned_scoped_and_current_without_treatment_authority(db, admin_user, variant):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import context_provenance
    from app.services.transaction_ops.scheduled_detection import receipt as build_receipt
    from tests.test_context_provenance import SCOPE, approve, decision, draft, propose, read
    from tests.test_transaction_ops_state import config_input

    actor = admin_user[0]
    request = draft(scope={**SCOPE.model_dump(), "currency": "EUR"}) if variant == "currency" else draft()
    content = request.model_dump(mode="json", exclude={"expected_version", "key"})
    selection = {
        "scope": request.scope.model_dump(),
        "key": request.key,
        "revision": 2 if variant == "revision" else 1,
        "content_sha256": "b" * 64 if variant == "hash" else state.business_digest(content),
    }
    if variant == "scope":
        selection["scope"]["posting_period_id"] = "101"
    config = await seed_config(
        db,
        actor.tenant_id,
        actor,
        netsuite_account_id="1234567_SB1",
        mapping_json={**config_input().mapping_json, "scheduled_context": selection},
    )
    if variant != "missing":
        await propose(db, actor, config, request)
    if variant not in {"missing", "draft"}:
        await approve(db, actor, config)
    if variant == "invalidated":
        await context_provenance.decide_context(
            db,
            actor.tenant_id,
            config.id,
            request.key,
            decision(await read(db, actor, config), kind="invalidate"),
            actor=actor,
        )
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="selected-context", order_references=[REF]),
        actor=actor,
        now=NOW,
    )
    if variant == "expired":
        observed = request.review_by + timedelta(seconds=1)
        receipt = await build_receipt(db, actor.tenant_id, run, config, evidence(), now=observed)
        assert receipt["accounting_context"]["status"] == "selected_context_requires_review"
        assert receipt["accounting_context"]["entries"][0]["status"] == "stale"
        assert receipt["outcome"] == "incomplete_evidence"
        return
    # Exercise the post-read guard independently; invalid selections cannot
    # now be activated for scheduled provider reads.
    receipt = await build_receipt(db, actor.tenant_id, run, config, evidence(), now=NOW)
    context = receipt["accounting_context"]
    assert context["selection"] == selection
    assert context["selection_current"] is (variant == "approved")
    assert context["policy_applied"] is False
    assert context["native_posting_scope_verified"] is False
    assert receipt["outcome"] == ("no_discrepancy" if variant == "approved" else "incomplete_evidence")
    assert receipt["version_semantics"]["sync_delay_proven"] is False
    assert receipt["financial_approval"] is None
    assert receipt["skill"]["applied"] is False
    assert "Synthetic reviewed policy example" not in str(receipt)
    assert not (await db.scalars(select(TransactionProposal))).all()
    assert not (await db.scalars(select(TransactionOperation))).all()
