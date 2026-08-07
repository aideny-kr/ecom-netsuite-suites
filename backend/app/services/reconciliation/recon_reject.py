"""Rejecting a match — the only path that produces NEGATIVE labels.

Every disposition the system has ever recorded is positive. `approved` is set in
two places (`api/v1/reconciliation.py`, `mcp/tools/recon_approve.py`); `rejected`
is set in none, despite being a declared `ResultStatus` and already treated as
terminal by `TERMINAL_RESULT_STATUSES`. So a wrong match leaves no trace, and the
false-positive rate of the autonomy envelope is unmeasurable *by construction* —
not hard to measure, impossible.

That matters because Rung 3 (unattended posting) rests entirely on the claim that
deterministic zero-variance matches are wrong rarely enough to post without a
human. This module is what makes that claim checkable. It has months of lead time
before the data is worth anything, which is why it ships long before posting does.

Two decisions carry the whole design:

1. NOT EVERY REJECT IS A FALSE POSITIVE. "These are not the same money" and "the
   match is right but I can't action it yet" are opposite evidence. Counting both
   against the envelope would let operational friction masquerade as model error
   and produce a confidently wrong number — worse than no number, because a
   number gets acted on. Only ``FALSE_POSITIVE_REASONS`` count.

2. ELIGIBILITY IS SNAPSHOTTED, NOT RECOMPUTED. The envelope's admission rules
   will change as the ladder is climbed. A rate computed by replaying today's
   rules over yesterday's rows would silently re-baseline itself every time the
   rules move, which is the failure mode where a metric improves because its
   definition drifted. We record what was true at decision time.

This module never writes to NetSuite. Reject is a local disposition and nothing
else — the HITL invariant holds here exactly as it does for approve.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reconciliation.four_bucket_classifier import (
    CLOSED_RUN_STATUSES,
    TERMINAL_RESULT_STATUSES,
)

# Why the match was rejected. A closed vocabulary, because a free-text-only
# reason cannot be aggregated and an unaggregatable label is not a label.
REJECT_REASONS: tuple[str, ...] = (
    "wrong_match",  # payout and deposit are not the same money
    "wrong_amount",  # right counterparty, but the amounts do not reconcile
    "duplicate",  # this deposit is already applied elsewhere
    "not_actionable",  # the match is CORRECT; something else blocks acting on it
    "other",  # requires a note
)

# The subset that is genuine evidence the matcher was WRONG. `not_actionable`
# is deliberately excluded: the match was right, so counting it would inflate the
# error rate with operational friction. `other` is excluded because an
# uncategorised reject cannot be assumed to be a model error — it is reviewed by
# a human and re-filed, not silently counted.
FALSE_POSITIVE_REASONS: tuple[str, ...] = (
    "wrong_match",
    "wrong_amount",
    "duplicate",
)


class RejectNotAllowedError(Exception):
    """The reject was refused. Message is safe to surface to the caller."""


def _is_envelope_eligible(result: Any) -> bool:
    """Would the autonomy envelope have admitted this row?

    Mirrors ``autonomy_envelope.evaluate``'s admission ladder deliberately rather
    than importing it: this is a HISTORICAL snapshot of what was true now, and it
    must not silently change meaning when the envelope's rules are widened.
    If the two ever diverge intentionally, that divergence is the point.
    """
    if result.status in TERMINAL_RESULT_STATUSES:
        return False
    if result.bucket != "matches":
        return False
    if result.match_type != "deterministic":
        return False
    # `evaluate` excludes amount-unknown rows explicitly ("must not be $0-blessed"),
    # and this ladder dropped that rung. stripe_amount is nullable, so a row the
    # envelope refuses outright was being snapshotted as eligible and then counted
    # as a false positive AGAINST the envelope — an error charged to a decision it
    # never made. Reproduced: evaluate() reported excluded={"amount_unknown": 1}
    # for a row this function called eligible.
    if getattr(result, "stripe_amount", None) is None:
        return False
    variance = result.variance_amount
    return variance is not None and Decimal(variance) == Decimal("0")


async def reject_result(
    db: AsyncSession,
    *,
    result: Any,
    user: Any,
    reason: str,
    note: str | None,
    run: Any,
    correlation_id: str | None = None,
) -> Any:
    """Record a human's judgement that this match is wrong (or unactionable).

    Inherits every invariant approve enforces — a closed run is a hard freeze and
    a terminal row is immutable — because a reject path that skipped them would
    be the back door into a frozen period.

    ``run`` is REQUIRED and has no default. It used to be ``run: Any | None = None``
    guarded by ``if run is not None``, which made the hard freeze opt-in: any caller
    that simply omitted the argument wrote into a closed period and no exception was
    raised. Reproduced — a run with status='closed' plus a call without ``run=``
    left the row 'rejected' inside a frozen period. The HTTP handler always passed
    it, so nothing was exploitable, but the repo already has a second approve path
    (``app/mcp/tools/recon_approve.py``) and a chat surface; the next caller is the
    one that would have found this. A guard whose enforcement depends on the caller
    remembering an optional argument is documentation, not a guard.
    """
    if reason not in REJECT_REASONS:
        raise RejectNotAllowedError(f"unknown reason {reason!r}; expected one of {', '.join(REJECT_REASONS)}")
    if reason == "other" and not (note or "").strip():
        raise RejectNotAllowedError("reason 'other' requires a note — an unexplained label is unreadable later")

    # No `is not None` escape: a missing run is a programming error, not a licence
    # to skip the freeze.
    if run is None:
        raise RejectNotAllowedError("internal: run is required to check the period freeze")
    if getattr(run, "status", None) in CLOSED_RUN_STATUSES:
        raise RejectNotAllowedError("run is closed — the period is frozen and dispositions cannot change")

    if result.status in TERMINAL_RESULT_STATUSES:
        raise RejectNotAllowedError(f"result cannot be rejected (status={result.status})")

    # Snapshot BEFORE mutating status — _is_envelope_eligible reads status, and
    # setting it to 'rejected' first would make every row ineligible and quietly
    # zero out the metric this whole module exists to produce.
    eligible = _is_envelope_eligible(result)

    result.status = "rejected"
    result.rejected_by = user.id
    result.rejected_at = datetime.now(timezone.utc)
    result.reject_reason = reason
    result.reject_note = (note or "").strip() or None
    result.envelope_eligible_at_decision = eligible
    result.counts_as_false_positive = eligible and reason in FALSE_POSITIVE_REASONS

    from app.services import audit_service

    await audit_service.log_event(
        db=db,
        tenant_id=result.tenant_id,
        category="reconciliation",
        action="recon.reject",
        actor_id=user.id,
        actor_type="user",
        resource_type="reconciliation_result",
        resource_id=str(result.id),
        correlation_id=correlation_id or str(uuid.uuid4()),
        payload={
            "reason": reason,
            "note": result.reject_note,
            "envelope_eligible_at_decision": eligible,
            "counts_as_false_positive": result.counts_as_false_positive,
            # Frozen alongside the decision so the label stays interpretable even
            # if the row is later re-classified.
            "bucket": result.bucket,
            "match_type": result.match_type,
            "variance_amount": str(result.variance_amount),
        },
    )
    return result


# ---------------------------------------------------------------------------
# envelope_false_positive_rate() WAS HERE. Removed 2026-08-06, before it was ever
# wired to anything, because it computed the number wrongly in two directions at
# once — and it is the number unattended posting is gated on. A metric that is
# merely absent blocks a decision; one that is confidently wrong justifies it.
#
# Both defects were reproduced against a real database:
#
#   DENOMINATOR TOO WIDE. It admitted every `approved` row on the stated grounds
#   that "approved rows were eligible by definition of the envelope's own ladder".
#   That is false: PATCH /results/{id}/approve checks only run-open and
#   non-terminal, so a fuzzy row with $500 variance — one `_is_envelope_eligible`
#   itself rejects — lands in the denominator. Measured: true rate 1/1, reported
#   0.1. The bias is toward "safe to post", which is the dangerous direction.
#
#   THE CORPUS MOVES AT CLOSE. close_period rewrites approved -> locked while
#   rejected rows keep their status forever, so month-end close silently deletes
#   every approval from the denominator. Measured: 0.05 -> 1.0 across a close,
#   with no change to the matcher.
#
# They push opposite ways, so they do not cancel; the number is uninterpretable at
# any instant. The fix is not a better WHERE clause — it is deciding what the
# denominator MEANS across a close boundary, and recording eligibility for
# approvals the way rejects already snapshot it, rather than inferring it after
# the fact from a status that another workflow is free to rewrite.
#
# The labels this module writes are correct and are the durable artifact; they
# accrue from today and any correct rate can be computed over them later.
# Tracked in ClickUp 86bb9nw0d.
