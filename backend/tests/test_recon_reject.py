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


import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.services.reconciliation.recon_reject import (
    FALSE_POSITIVE_REASONS,
    REJECT_REASONS,
    RejectNotAllowedError,
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
    run, r = await _result(db, tenant_a.id)

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note="different customer", run=run)

    assert r.status == "rejected"
    assert r.rejected_by == user_a.id
    assert r.rejected_at is not None
    assert r.reject_reason == "wrong_match"
    assert r.reject_note == "different customer"


async def test_reject_rejects_an_unknown_reason(db, tenant_a, user_a):
    """Free-text-only reasons cannot be aggregated, and an unaggregatable label
    is not a label."""
    run, r = await _result(db, tenant_a.id)

    with pytest.raises(RejectNotAllowedError, match="reason"):
        await reject_result(db, result=r, user=user_a, reason="because I said so", note=None, run=run)


async def test_other_requires_a_note(db, tenant_a, user_a):
    """'other' without an explanation is an unreadable label six months later."""
    run, r = await _result(db, tenant_a.id)

    with pytest.raises(RejectNotAllowedError, match="note"):
        await reject_result(db, result=r, user=user_a, reason="other", note=None, run=run)


# --- envelope eligibility is SNAPSHOTTED, not recomputed ------------------


async def test_snapshots_envelope_eligibility_for_an_admissible_row(db, tenant_a, user_a):
    """This row is exactly what Rung 3 would have auto-approved. Rejecting it is
    the single most valuable label the system can produce."""
    from decimal import Decimal

    run, r = await _result(
        db,
        tenant_a.id,
        status="auto_matched",
        bucket="matches",
        match_type="deterministic",
        variance_amount=Decimal("0"),
    )

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None, run=run)

    assert r.envelope_eligible_at_decision is True
    assert r.counts_as_false_positive is True


async def test_snapshots_ineligibility_for_a_variance_row(db, tenant_a, user_a):
    from decimal import Decimal

    run, r = await _result(
        db,
        tenant_a.id,
        status="needs_review",
        bucket="needs_review",
        match_type="fuzzy",
        variance_amount=Decimal("12.50"),
    )

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None, run=run)

    assert r.envelope_eligible_at_decision is False
    # still a false-positive REASON, but not against the envelope
    assert r.counts_as_false_positive is False


async def test_not_actionable_never_counts_against_the_envelope(db, tenant_a, user_a):
    from decimal import Decimal

    run, r = await _result(
        db,
        tenant_a.id,
        status="auto_matched",
        bucket="matches",
        match_type="deterministic",
        variance_amount=Decimal("0"),
    )

    await reject_result(db, result=r, user=user_a, reason="not_actionable", note="waiting on bank", run=run)

    assert r.envelope_eligible_at_decision is True
    assert r.counts_as_false_positive is False, "operational friction is not model error"


# --- the invariants reject must inherit from approve ----------------------


async def test_cannot_reject_inside_a_closed_run(db, tenant_a, user_a):
    """Close is a HARD FREEZE. Approve enforces this; reject must not be the
    back door that mutates a frozen period."""
    run = await create_test_recon_run(db, tenant_a.id, status="closed")
    r = await create_test_recon_result(db, tenant_a.id, run.id)
    await db.flush()

    with pytest.raises(RejectNotAllowedError, match="closed|locked|freeze"):
        await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None, run=run)


@pytest.mark.parametrize("terminal", ["approved", "locked", "carried_forward", "rejected"])
async def test_cannot_reject_a_terminal_row(db, tenant_a, user_a, terminal):
    run, r = await _result(db, tenant_a.id, status=terminal)

    with pytest.raises(RejectNotAllowedError):
        await reject_result(db, result=r, user=user_a, reason="wrong_match", note=None, run=run)


async def test_reject_never_posts_to_netsuite(db, tenant_a, user_a):
    """HITL invariant: no reconciliation action writes to NetSuite. Reject is a
    local disposition and nothing else."""
    import app.services.reconciliation.recon_reject as mod

    src = mod.__file__ or ""
    assert src
    text = open(src).read()
    for forbidden in ("createRecord", "ns_createRecord", "updateRecord", "post_to_netsuite"):
        assert forbidden not in text, f"reject path must never reach NetSuite ({forbidden})"


async def test_reject_writes_one_audit_row(db, tenant_a, user_a):
    run, r = await _result(db, tenant_a.id)

    await reject_result(db, result=r, user=user_a, reason="wrong_match", note="n", run=run)
    await db.flush()

    rows = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.action == "recon.reject", AuditEvent.resource_id == str(r.id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "exactly one audit row per rejected line — this is the audit trail"
    assert rows[0].actor_id == user_a.id


# --- the rate itself is NOT computed here ---------------------------------
# Two tests lived here asserting envelope_false_positive_rate() returned 0.25 on a
# 4-row fixture, and both passed. The function was still wrong: the fixture only
# ever approved rows that WERE envelope-eligible, so it never exercised the case
# that broke it — an approved row the envelope would refuse, which the endpoint
# happily allows. The tests encoded the same assumption as the code, so they
# confirmed it rather than checking it.
#
# That is the failure worth remembering: a green test written from the
# implementation's own model of the world proves only that the model is
# self-consistent. The defect was found by asking what ELSE can set status
# 'approved', and the answer was "an endpoint with no envelope check at all".
#
# The labels below are the durable artifact and they are correct. Any rate is a
# pure function of them and can be computed — correctly — later.


# --- guards that were opt-in or missing, each from a reproduced review finding ---


async def test_amount_unknown_row_is_not_envelope_eligible(db, tenant_a, user_a):
    """A row the envelope refuses must not be snapshotted as eligible.

    `autonomy_envelope.evaluate` excludes amount-unknown rows explicitly so they are
    not "$0-blessed"; this ladder had dropped that rung. The effect was an error
    charged against a decision the envelope never made — corpus corruption that
    reads as a real matcher failure.
    """
    from decimal import Decimal

    from app.services.reconciliation.recon_reject import _is_envelope_eligible

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    row = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="auto_matched",
        bucket="matches",
        match_type="deterministic",
        variance_amount=Decimal("0"),
    )
    row.stripe_amount = None
    await db.flush()

    assert _is_envelope_eligible(row) is False

    await reject_result(db, result=row, user=user_a, reason="wrong_match", note=None, run=run)
    await db.flush()
    assert row.envelope_eligible_at_decision is False
    assert row.counts_as_false_positive is False


async def test_run_is_required_so_the_freeze_cannot_be_skipped(db, tenant_a, user_a):
    """`run` had a None default guarded by `if run is not None`, so omitting it
    silently skipped the period freeze. A guard that depends on the caller
    remembering an optional argument is documentation, not a guard."""
    import inspect

    from app.services.reconciliation.recon_reject import reject_result as _rr

    assert inspect.signature(_rr).parameters["run"].default is inspect.Parameter.empty

    run = await create_test_recon_run(db, tenant_a.id, status="closed")
    row = await create_test_recon_result(db, tenant_a.id, run.id, status="suggested")
    await db.flush()

    with pytest.raises(RejectNotAllowedError, match="closed"):
        await reject_result(db, result=row, user=user_a, reason="wrong_match", note=None, run=run)
    assert row.status == "suggested"

    # And an explicit None is refused rather than treated as "no freeze to check".
    with pytest.raises(RejectNotAllowedError):
        await reject_result(db, result=row, user=user_a, reason="wrong_match", note=None, run=None)
    assert row.status == "suggested"
