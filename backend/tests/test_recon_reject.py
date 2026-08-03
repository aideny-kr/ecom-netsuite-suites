"""Reject a reconciliation match — the negative-label path.

WHY THIS EXISTS (read before changing any of it):

Unattended posting (Rung 3) rests on a claim nobody can currently evaluate:
"deterministic, zero-variance matches are wrong less than 1-in-N times". The
autonomy envelope admits exactly those rows (bucket=matches, match_type=
deterministic, variance=0, non-terminal). But the system has only ever recorded
POSITIVE outcomes — `approved` is set in two places, `rejected` in none — so a
false positive leaves no trace and the rate is unmeasurable by construction.

This endpoint is how negative labels start accumulating. It has months of lead
time before it is worth anything, which is why it ships long before posting does.

The load-bearing design decision is the reason taxonomy. A reject that means
"this match is wrong" and a reject that means "the match is right but I can't
action it" are opposite evidence. Conflating them makes the false-positive rate
worse than useless — it produces a confidently wrong number. Only
FALSE_POSITIVE_REASONS count against the envelope.

Second decision: `envelope_eligible_at_decision` is snapshotted at reject time
rather than recomputed later. The envelope's admission rules WILL change; a rate
computed by replaying today's rules over yesterday's rows would silently
re-baseline itself. History not captured now is blank forever.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.services.reconciliation.recon_reject import (
    FALSE_POSITIVE_REASONS,
    REJECT_REASONS,
    RejectNotAllowed,
    reject_result,
)
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


@pytest.fixture
async def user_a(db, tenant_a):
    """The reviewer making the disposition. admin_user returns (User, headers);
    every test here only needs the User."""
    u, _ = await create_test_user(db, tenant_a)
    await db.flush()
    return u


async def _result(db, tenant_id, **kw):
    run = await create_test_recon_run(db, tenant_id, status="completed")
    r = await create_test_recon_result(db, tenant_id, run.id, **kw)
    await db.flush()
    return run, r


# --- the taxonomy is the whole point -------------------------------------


def test_reason_taxonomy_separates_false_positives_from_unactionable():
    """A reject meaning 'this match is wrong' and one meaning 'the match is
    right but I cannot action it' are opposite evidence. If every reason counted
    against the envelope, operational friction would masquerade as model error."""
    assert FALSE_POSITIVE_REASONS, "at least one reason must mark a genuine false positive"
    assert set(FALSE_POSITIVE_REASONS).issubset(set(REJECT_REASONS))
    # not_actionable is the canonical NON-false-positive: the match was correct.
    assert "not_actionable" in REJECT_REASONS
    assert "not_actionable" not in FALSE_POSITIVE_REASONS
    # wrong_match is the canonical false positive: not the same money.
    assert "wrong_match" in FALSE_POSITIVE_REASONS


async def test_reject_records_who_when_why(db, tenant_a, user_a):
    _, r = await _result(db, tenant_a.id)

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note="different customer")

    assert r.status == "rejected"
    assert r.rejected_by == user_a.id
    assert r.rejected_at is not None
    assert r.reject_reason == "wrong_match"
    assert r.reject_note == "different customer"


async def test_reject_rejects_an_unknown_reason(db, tenant_a, user_a):
    """Free-text-only reasons cannot be aggregated, and an unaggregatable label
    is not a label."""
    _, r = await _result(db, tenant_a.id)

    with pytest.raises(RejectNotAllowed, match="reason"):
        await reject_result(db, result=r, user=user_a, reason="because I said so", note=None)


async def test_other_requires_a_note(db, tenant_a, user_a):
    """'other' without an explanation is an unreadable label six months later."""
    _, r = await _result(db, tenant_a.id)

    with pytest.raises(RejectNotAllowed, match="note"):
        await reject_result(db, result=r, user=user_a, reason="other", note=None)


# --- envelope eligibility is SNAPSHOTTED, not recomputed ------------------


async def test_snapshots_envelope_eligibility_for_an_admissible_row(db, tenant_a, user_a):
    """This row is exactly what Rung 3 would have auto-approved. Rejecting it is
    the single most valuable label the system can produce."""
    from decimal import Decimal

    _, r = await _result(
        db, tenant_a.id, status="auto_matched", bucket="matches", match_type="deterministic",
        variance_amount=Decimal("0"),
    )

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None)

    assert r.envelope_eligible_at_decision is True
    assert r.counts_as_false_positive is True


async def test_snapshots_ineligibility_for_a_variance_row(db, tenant_a, user_a):
    from decimal import Decimal

    _, r = await _result(
        db, tenant_a.id, status="needs_review", bucket="needs_review", match_type="fuzzy",
        variance_amount=Decimal("12.50"),
    )

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None)

    assert r.envelope_eligible_at_decision is False
    # still a false-positive REASON, but not against the envelope
    assert r.counts_as_false_positive is False


async def test_not_actionable_never_counts_against_the_envelope(db, tenant_a, user_a):
    from decimal import Decimal

    _, r = await _result(
        db, tenant_a.id, status="auto_matched", bucket="matches", match_type="deterministic",
        variance_amount=Decimal("0"),
    )

    await reject_result(db, result=r, user=user_a, reason="not_actionable", note="waiting on bank")

    assert r.envelope_eligible_at_decision is True
    assert r.counts_as_false_positive is False, "operational friction is not model error"


# --- the invariants reject must inherit from approve ----------------------


async def test_cannot_reject_inside_a_closed_run(db, tenant_a, user_a):
    """Close is a HARD FREEZE. Approve enforces this; reject must not be the
    back door that mutates a frozen period."""
    run = await create_test_recon_run(db, tenant_a.id, status="closed")
    r = await create_test_recon_result(db, tenant_a.id, run.id)
    await db.flush()

    with pytest.raises(RejectNotAllowed, match="closed|locked|freeze"):
        await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None, run=run)


@pytest.mark.parametrize("terminal", ["approved", "locked", "carried_forward", "rejected"])
async def test_cannot_reject_a_terminal_row(db, tenant_a, user_a, terminal):
    _, r = await _result(db, tenant_a.id, status=terminal)

    with pytest.raises(RejectNotAllowed):
        await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None)


async def test_reject_never_posts_to_netsuite(db, tenant_a, user_a):
    """HITL invariant: no reconciliation action writes to NetSuite. Reject is a
    local disposition and nothing else."""
    import app.services.reconciliation.recon_reject as mod

    src = (mod.__file__ or "")
    assert src
    text = open(src).read()
    for forbidden in ("createRecord", "ns_createRecord", "updateRecord", "post_to_netsuite"):
        assert forbidden not in text, f"reject path must never reach NetSuite ({forbidden})"


async def test_reject_writes_one_audit_row(db, tenant_a, user_a):
    _, r = await _result(db, tenant_a.id)

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note="n")
    await db.flush()

    rows = (
        (await db.execute(select(AuditEvent).where(AuditEvent.action == "recon.reject",
                                                   AuditEvent.resource_id == str(r.id))))
        .scalars().all()
    )
    assert len(rows) == 1, "exactly one audit row per rejected line — this is the audit trail"
    assert rows[0].actor_id == user_a.id


# --- the payoff: a rate you can actually compute --------------------------


async def test_false_positive_rate_is_computable(db, tenant_a, user_a):
    """The whole point. Rung 3 cannot be argued for or against without this."""
    from decimal import Decimal

    from app.services.reconciliation.recon_reject import envelope_false_positive_rate

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    for i in range(4):
        row = await create_test_recon_result(
            db, tenant_a.id, run.id, status="auto_matched", bucket="matches",
            match_type="deterministic", variance_amount=Decimal("0"),
        )
        await db.flush()
        if i == 0:
            await reject_result(db, result=row, user=user_a, reason="wrong_match", note=None)
        elif i == 1:
            await reject_result(db, result=row, user=user_a, reason="not_actionable", note="x")
        else:
            row.status = "approved"
            row.approved_by = user_a.id
            row.approved_at = datetime.now(timezone.utc)
    await db.flush()

    rate = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)

    # 4 envelope-eligible decided rows, exactly 1 a genuine false positive.
    assert rate["decided"] == 4
    assert rate["false_positives"] == 1
    assert rate["rate"] == pytest.approx(0.25)


async def test_rate_is_none_when_there_is_no_evidence(db, tenant_a):
    """With no decided rows the honest answer is 'unknown', not 0.0. A confident
    zero here is exactly how an unsafe autonomy decision gets justified."""
    from app.services.reconciliation.recon_reject import envelope_false_positive_rate

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    rate = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)

    assert rate["decided"] == 0
    assert rate["rate"] is None
