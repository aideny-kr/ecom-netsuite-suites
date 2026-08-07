"""PATCH /api/v1/reconciliation/results/{result_id}/reject — the negative-label endpoint.

`recon_reject.reject_result` has been tested since it was written, but nothing
exposed it, so no label could ever be recorded and the false-positive rate had no
corpus to measure. These tests cover the HTTP seam specifically: tenant scoping,
the closed-period freeze, terminal-status immutability, and the fact that the
envelope snapshot survives the round trip to the database.

The reject path deliberately inherits every guard `approve` enforces. A reject
that skipped the close-lock would be a back door into a frozen period, so the
freeze tests here are the point of the file, not an afterthought.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.reconciliation import ReconciliationResult
from tests.conftest import create_test_recon_result, create_test_recon_run

pytestmark = pytest.mark.asyncio

REJECT_URL = "/api/v1/reconciliation/results/{}/reject"


async def test_reject_marks_result_and_persists_reason(client, db, finance_user):
    """The happy path: status flips to rejected and the label columns are written."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, status="suggested")
    await db.commit()

    resp = await client.patch(
        REJECT_URL.format(res.id),
        json={"reason": "wrong_match", "note": "payout belongs to the March batch"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    await db.refresh(res)
    assert res.status == "rejected"
    assert res.reject_reason == "wrong_match"
    assert res.reject_note == "payout belongs to the March batch"
    assert res.rejected_by == user.id
    assert res.rejected_at is not None


async def test_reject_snapshots_envelope_eligibility_as_false_positive(client, db, finance_user):
    """A deterministic, zero-variance match rejected as wrong_match IS a false positive.

    This is the whole point of the endpoint: that row is exactly what the autonomy
    envelope would have admitted for unattended posting, so a human calling it wrong
    is the evidence Rung 3 is gated on.
    """
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(
        db,
        user.tenant_id,
        run.id,
        status="suggested",
        match_type="deterministic",
        variance_amount=Decimal("0"),
    )
    await db.commit()

    resp = await client.patch(REJECT_URL.format(res.id), json={"reason": "wrong_match"}, headers=headers)
    assert resp.status_code == 200, resp.text

    await db.refresh(res)
    assert res.envelope_eligible_at_decision is True
    assert res.counts_as_false_positive is True


async def test_not_actionable_is_not_a_false_positive(client, db, finance_user):
    """`not_actionable` means the match was RIGHT — counting it would inflate the error rate."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(
        db,
        user.tenant_id,
        run.id,
        status="suggested",
        match_type="deterministic",
        variance_amount=Decimal("0"),
    )
    await db.commit()

    resp = await client.patch(
        REJECT_URL.format(res.id),
        json={"reason": "not_actionable", "note": "vendor dispute open"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    await db.refresh(res)
    assert res.envelope_eligible_at_decision is True
    assert res.counts_as_false_positive is False


async def test_reason_other_requires_a_note(client, db, finance_user):
    """An uncategorised reject with no note is unreadable later, so it is refused."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, status="suggested")
    await db.commit()

    resp = await client.patch(REJECT_URL.format(res.id), json={"reason": "other"}, headers=headers)
    assert resp.status_code == 400
    assert "note" in resp.json()["detail"].lower()

    await db.refresh(res)
    assert res.status == "suggested"  # unchanged


async def test_unknown_reason_is_refused(client, db, finance_user):
    """The vocabulary is closed — an unaggregatable label is not a label."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, status="suggested")
    await db.commit()

    resp = await client.patch(REJECT_URL.format(res.id), json={"reason": "seems_off"}, headers=headers)
    assert resp.status_code in (400, 422)

    await db.refresh(res)
    assert res.status == "suggested"


async def test_closed_run_is_a_hard_freeze(client, db, finance_user):
    """Close means frozen. A reject must not be a back door into a closed period."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id, status="closed")
    res = await create_test_recon_result(db, user.tenant_id, run.id, status="suggested")
    await db.commit()

    resp = await client.patch(REJECT_URL.format(res.id), json={"reason": "wrong_match"}, headers=headers)
    assert resp.status_code == 400

    await db.refresh(res)
    assert res.status == "suggested"
    assert res.reject_reason is None


async def test_already_approved_result_cannot_be_rejected(client, db, finance_user):
    """Terminal rows are immutable — flipping one would rewrite a decision already made."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, status="approved")
    await db.commit()

    resp = await client.patch(REJECT_URL.format(res.id), json={"reason": "wrong_match"}, headers=headers)
    assert resp.status_code == 400

    await db.refresh(res)
    assert res.status == "approved"


async def test_reject_is_tenant_scoped(client, db, finance_user, tenant_b):
    """Another tenant's result is a 404, never a 403 — existence itself is not disclosed."""
    user, headers = finance_user
    other_run = await create_test_recon_run(db, tenant_b.id)
    other_res = await create_test_recon_result(db, tenant_b.id, other_run.id, status="suggested")
    await db.commit()

    resp = await client.patch(REJECT_URL.format(other_res.id), json={"reason": "wrong_match"}, headers=headers)
    assert resp.status_code == 404

    refreshed = (
        await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == other_res.id))
    ).scalar_one()
    assert refreshed == "suggested"


async def test_reject_writes_an_audit_event_with_the_snapshot(client, db, finance_user):
    """Per-line audit is the HITL invariant: the label must be reconstructable later."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(
        db,
        user.tenant_id,
        run.id,
        status="suggested",
        match_type="deterministic",
        variance_amount=Decimal("0"),
    )
    await db.commit()

    resp = await client.patch(
        REJECT_URL.format(res.id),
        json={"reason": "duplicate", "note": "already applied on deposit 8812"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "recon.reject",
                AuditEvent.resource_type == "reconciliation_result",
                AuditEvent.resource_id == str(res.id),
            )
        )
    ).scalar_one()
    assert event.payload["reason"] == "duplicate"
    assert event.payload["note"] == "already applied on deposit 8812"
    assert event.payload["envelope_eligible_at_decision"] is True
    assert event.payload["counts_as_false_positive"] is True
    assert event.actor_id == user.id


async def test_missing_result_is_404(client, db, finance_user):
    """A well-formed uuid that does not exist is a 404, not a 500."""
    _user, headers = finance_user
    resp = await client.patch(
        REJECT_URL.format("00000000-0000-0000-0000-000000000000"),
        json={"reason": "wrong_match"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_malformed_uuid_is_not_a_500(client, db, finance_user):
    """`_parse_uuid` exists precisely so a bad path param is a 4xx."""
    _user, headers = finance_user
    resp = await client.patch(REJECT_URL.format("not-a-uuid"), json={"reason": "wrong_match"}, headers=headers)
    assert resp.status_code in (400, 404, 422)
