"""What was true when a human DECIDED a row, and the rate computed from it.

The autonomy envelope's false-positive rate answers one question: is the matcher
wrong rarely enough to post without a human. That is a property of the matcher over
time, so the corpus is cumulative — a decision made in April still counts in June,
and closing April does not change it (design decision 2026-08-07, Option A).

Which forces one structural choice: **the denominator keys on a snapshot column, not
on `status`.** An earlier version keyed on status and was wrong in two directions at
once, both reproduced against a real database:

  · it admitted every `approved` row on the premise that approve enforces the
    envelope. It does not — approve checks only run-open and non-terminal — so a
    fuzzy row with $500 variance counted. True 1/1 reported as 0.1, and the bias
    was toward "safe to post".
  · `close_period` rewrites approved -> locked while rejected rows keep their status
    forever, so a month-end close deleted every approval from the denominator:
    0.05 -> 1.0 with the matcher untouched.

A status is a mutable workflow position that other features are free to rewrite. A
snapshot is a fact about a moment. Only the second can carry evidence.

THE LADDER EXISTS TWICE, AND THAT IS DELIBERATE. Three of the decide paths are
set-based UPDATEs that never load an ORM object (bulk bucket approve, resolution
group approve, and any future one), so eligibility must be expressible in SQL. Two
representations of one rule is exactly how rules drift — silently, because each is
individually tested. The rungs are therefore declared ONCE, as data, and both forms
are generated from that list; `test_python_and_sql_ladders_agree` crosses every rung
and fails if they ever disagree.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select

from app.models.reconciliation import ReconciliationResult as R
from app.services.reconciliation.four_bucket_classifier import TERMINAL_RESULT_STATUSES

# The admission ladder, declared once. Each rung is (name, python_check, sql_check).
#
# It mirrors `autonomy_envelope.evaluate` rather than importing it, because this is a
# HISTORICAL judgement: it must not change meaning when the envelope's rules are
# widened. If the two ever diverge intentionally, that divergence is the point — but
# an UNINTENDED divergence already happened once (the amount-unknown rung was missing
# here, so rows the envelope refuses were snapshotted eligible and counted as errors
# against a decision it never made).
_RUNGS: list[tuple[str, Any, Any]] = [
    (
        "not already decided",
        lambda r: r.status not in TERMINAL_RESULT_STATUSES,
        lambda: R.status.notin_(TERMINAL_RESULT_STATUSES),
    ),
    ("matched bucket", lambda r: r.bucket == "matches", lambda: R.bucket == "matches"),
    (
        "deterministic",
        lambda r: r.match_type == "deterministic",
        lambda: R.match_type == "deterministic",
    ),
    # Amount-unknown rows must not be "$0-blessed": a NULL amount is not a zero
    # variance, and `evaluate` excludes them explicitly.
    (
        "amount known",
        lambda r: getattr(r, "stripe_amount", None) is not None,
        lambda: R.stripe_amount.isnot(None),
    ),
    (
        "zero variance",
        lambda r: r.variance_amount is not None and Decimal(r.variance_amount) == Decimal("0"),
        lambda: R.variance_amount == 0,
    ),
]


def is_envelope_eligible(result: Any) -> bool:
    """Would the envelope have admitted this row, as of right now?"""
    return all(check(result) for _, check, _ in _RUNGS)


def eligible_sql():
    """The same ladder as a SQL predicate, for set-based decide paths."""
    return and_(*(sql() for _, _, sql in _RUNGS))


def record_decision_snapshot(result: Any, *, false_positive: bool = False) -> bool:
    """Freeze what was true at the moment this row was decided. Call BEFORE mutating
    status — the first rung reads status, so setting it to a terminal value first
    makes every row ineligible and silently zeroes the metric.

    Returns the eligibility it recorded. `false_positive` is only ever True on the
    reject path: an approval means the human agreed with the matcher, so it is
    evidence the envelope was RIGHT and belongs in the denominator, never the
    numerator.
    """
    eligible = is_envelope_eligible(result)
    result.envelope_eligible_at_decision = eligible
    result.counts_as_false_positive = bool(eligible and false_positive)
    return eligible


async def envelope_false_positive_rate(db, *, tenant_id: Any, run_id: Any | None = None) -> dict[str, Any]:
    """Of the rows the envelope would have posted unattended, how many did a human
    call a genuine matcher error?

    Both halves key on the SNAPSHOT, never on `status`, so no later workflow — close,
    lock, re-run — can move the number without a human deciding something.
    """
    where = [R.tenant_id == tenant_id]
    if run_id is not None:
        where.append(R.run_id == run_id)

    # A non-NULL snapshot IS the record that a human decided this row; that is why the
    # column is nullable and why nothing else writes it.
    decided = (
        await db.execute(select(func.count()).select_from(R).where(*where, R.envelope_eligible_at_decision.is_(True)))
    ).scalar_one()

    false_positives = (
        await db.execute(select(func.count()).select_from(R).where(*where, R.counts_as_false_positive.is_(True)))
    ).scalar_one()

    decided = int(decided or 0)
    false_positives = int(false_positives or 0)
    return {
        "decided": decided,
        "false_positives": false_positives,
        # None, never 0.0, on an empty corpus. A confident zero here is exactly how an
        # unsafe autonomy decision gets justified: "no evidence" must not read as
        # "no errors".
        "rate": (false_positives / decided) if decided else None,
    }
