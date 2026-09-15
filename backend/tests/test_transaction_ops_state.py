"""Persistent transaction work must survive retries and require a real human decision."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.transaction_runs import ConfigCreate, FindingReport, ProposalCreate, RunCreate
from app.services.transaction_ops.state_service import business_digest

NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def config_input(**changes):
    return ConfigCreate(
        **{
            "name": "Framework UK",
            "source_step_id": uuid4(),
            "netsuite_connection_id": uuid4(),
            "netsuite_account_id": "6738075_SB1",
            "subsidiary_id": "5",
            "mapping_json": {
                "reference_field": "tranid",
                "currency_minor_units": {"GBP": 2},
                "action_mode": "propose_actions",
            },
            **changes,
        }
    )


def proposal_input(**changes):
    return ProposalCreate(
        **{
            "source_record_id": "framework-order-1",
            "order_reference": "R123456789",
            "action": "correct_amounts",
            "currency": "GBP",
            "evidence_fingerprint": "a" * 64,
            "observed_at": NOW,
            "before_json": {"total": "120.00"},
            "after_json": {"total": "121.00"},
            "evidence_json": {"findings": ["total_mismatch"]},
            **changes,
        }
    )


def test_schedule_is_opt_in_and_mapping_survives_without_guessed_defaults():
    config = config_input()
    assert config.schedule_enabled is False
    assert config.record_type == "salesorder"
    assert config.mapping_json == {
        "reference_field": "tranid",
        "currency_minor_units": {"GBP": 2},
        "action_mode": "propose_actions",
    }


def test_run_refs_normalized_for_stable_business_scope():
    run = RunCreate(evaluation_key="chat-message-1", order_references=["R123456789", "R123456780", "R123456789"])
    assert run.order_references == ("R123456780", "R123456789")


@pytest.mark.parametrize(
    "changes",
    [{}, {"order_references": ["../other"]}, {"window_start": NOW}, {"window_start": NOW, "window_end": NOW}],
)
def test_run_requires_an_exact_bounded_scope(changes):
    with pytest.raises(ValidationError):
        RunCreate(evaluation_key="request-1", **changes)


def test_window_clock_must_be_aware_and_ordered():
    with pytest.raises(ValidationError):
        RunCreate(evaluation_key="schedule-1", window_start=NOW.replace(tzinfo=None), window_end=NOW)
    with pytest.raises(ValidationError):
        RunCreate(evaluation_key="schedule-1", window_start=NOW, window_end=NOW + timedelta(days=32))


@pytest.mark.parametrize("value", [1.25, float("nan"), {"nested": float("inf")}])
def test_evidence_never_persists_binary_floats(value):
    with pytest.raises(ValidationError):
        proposal_input(after_json={"total": value})


def test_payload_bounds_and_actor_spoofing():
    with pytest.raises(ValidationError):
        proposal_input(after_json={"total": "1" * 65537})
    with pytest.raises(ValidationError):
        proposal_input(approved_by=str(uuid4()))
    with pytest.raises(ValidationError):
        RunCreate(evaluation_key="request", order_references=["R123456789"], initiated_by=str(uuid4()))


def test_digest_is_exact_canonical_and_dict_order_independent():
    assert business_digest({"total": Decimal("120.00"), "observed": NOW}) == business_digest(
        {"observed": NOW.astimezone(timezone(timedelta(hours=8))), "total": Decimal("120.000")}
    )
    assert business_digest({"amount": "0.001"}) != business_digest({"amount": "0.002"})


def test_observation_reports_require_full_reference_and_bounded_json():
    assert FindingReport(order_reference="R123456789", report_json={"status": "no_action"}).report_json
    with pytest.raises(ValidationError):
        FindingReport(order_reference="R123", report_json={"status": "no_action"})
    with pytest.raises(ValidationError):
        FindingReport(order_reference="R123456789", report_json={"raw": "x" * 65537})
