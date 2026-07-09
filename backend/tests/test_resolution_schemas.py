"""Schema/model contracts for resolution proposals."""

from decimal import Decimal
from typing import get_args

from app.models.reconciliation import ACTIVE_PROPOSAL_STATUSES, ReconResolutionProposal  # noqa: F401
from app.schemas.reconciliation import (
    PostFailureReason,
    ProposalStatus,
    ResolutionAction,
    ResolutionProposalResponse,
    ResultStatus,
)
from app.services.reconciliation.four_bucket_classifier import (
    TERMINAL_RESULT_STATUSES,
    is_material,
)


def test_resolution_action_values():
    assert set(get_args(ResolutionAction)) == {
        "book_fee_line",
        "create_and_apply_deposit",
        "apply_deposit",
        "credit_memo_refund",
        "void_duplicate",
        "writeoff_je",
        "carry_forward",
        "needs_human",
    }


def test_proposal_status_values():
    assert set(get_args(ProposalStatus)) == {
        "proposed",
        "approved",
        "posting",
        "posted",
        "rejected",
        "post_failed",
        "superseded",
    }
    assert set(ACTIVE_PROPOSAL_STATUSES) == {"proposed", "approved", "posting", "posted", "post_failed"}


def test_failure_reason_values():
    assert set(get_args(PostFailureReason)) == {
        "period_locked",
        "period_closed",
        "connection",
        "netsuite_validation",
        "netsuite_error",
        "guard_tripped",
    }


def test_result_status_gains_carried_forward():
    assert "carried_forward" in get_args(ResultStatus)
    assert "carried_forward" in TERMINAL_RESULT_STATUSES  # bulk-approve must skip it


def test_is_material_public_helper():
    # $50 abs / 1% pct defaults: $60 variance on a $10k order is material (abs);
    # $40 on a $100 order is material (pct: >$1); $0.40 on a $100 order is not.
    assert is_material(Decimal("60"), Decimal("10000"), Decimal("50"), Decimal("0.01")) is True
    assert is_material(Decimal("40"), Decimal("100"), Decimal("50"), Decimal("0.01")) is True
    assert is_material(Decimal("0.40"), Decimal("100"), Decimal("50"), Decimal("0.01")) is False


def test_proposal_response_from_orm_shape():
    fields = ResolutionProposalResponse.model_fields
    for f in (
        "id",
        "run_id",
        "result_id",
        "root_cause",
        "action",
        "booking_vehicle",
        "group_key",
        "source",
        "narrative",
        "proposed_amount",
        "currency",
        "above_materiality",
        "status",
        "failure_reason",
        "correlation_id",
    ):
        assert f in fields
