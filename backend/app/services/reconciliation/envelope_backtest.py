"""Does the envelope contradict itself? A 100%-coverage LOWER bound, no humans needed.

The envelope claims a class of matches is safe to post unattended. Measuring how often
that claim is WRONG needs human verification of a random sample, which does not exist
yet. But one thing can be checked today, over every row, for the cost of a query:

    when reconciliation runs twice over the same window, does it pair the same order
    to two different deposits while claiming envelope-grade confidence both times?

Two envelope-grade answers that disagree means at least one was wrong. So this
counts REAL errors — never hypothetical ones — and it needs no reviewer, which is
what makes it the free companion to the sampled estimate:

    this back-test  -> strict LOWER bound  (only errors that contradicted themselves)
    sampled review  -> upper bound with a confidence interval

Ship both. If the back-test ever exceeds the sampled upper bound, the SAMPLE is
broken — a permanent, free alarm on the measurement apparatus itself.

WHAT A ZERO HERE DOES NOT MEAN. Self-consistency is not correctness. A matcher that
is reliably wrong — an ambiguous order reference that always resolves to the same
wrong deposit — scores a perfect zero. That is exactly the specification error only
human sampling catches. The return value therefore carries `is_lower_bound=True` and
`measures` describing the estimand, so no caller can read 0.0 as "the matcher is
correct". First run on production data (2026-08-09): 5,168 eligible orders re-run over
the same window, 1,377 deposit disagreements overall, and ZERO where both sides
claimed envelope grade — consistent with later runs simply seeing NetSuite deposits
that had not synced yet, which is the envelope declining to claim certainty rather
than getting it wrong.

Re-runs are also not a random sample of anything: which windows get re-run is an
operational accident. This is a DIAGNOSTIC over whatever happens to have been
re-reconciled, not an estimate of a population rate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

# Envelope grade, inline in SQL: this deliberately mirrors recon_decision's ladder
# rather than importing it, for the same reason the snapshot does — it is a statement
# about what was true in a past run, and must not silently change meaning when the
# envelope widens. If they diverge intentionally, that divergence is the point.
_SQL = """
WITH graded AS (
    SELECT r.date_from,
           r.date_to,
           x.run_id,
           x.evidence->>'order_reference' AS oref,
           x.deposit_id,
           (x.match_type = 'deterministic'
            AND x.bucket = 'matches'
            AND x.variance_amount = 0
            AND x.stripe_amount IS NOT NULL) AS envelope_grade
    FROM reconciliation_results x
    JOIN reconciliation_runs r ON r.id = x.run_id
    WHERE x.tenant_id = :tenant_id
      AND x.evidence->>'order_reference' IS NOT NULL
      AND x.deposit_id IS NOT NULL
),
grouped AS (
    SELECT date_from,
           date_to,
           oref,
           COUNT(DISTINCT run_id) AS runs,
           BOOL_OR(envelope_grade) AS ever_graded,
           COUNT(DISTINCT deposit_id) AS deposits,
           -- the load-bearing one: distinct deposits among runs that BOTH claimed
           -- envelope grade. Anything else has an ungraded side, which is the
           -- envelope correctly declining to be certain.
           COUNT(DISTINCT deposit_id) FILTER (WHERE envelope_grade) AS graded_deposits
    FROM graded
    GROUP BY date_from, date_to, oref
)
SELECT
    COUNT(*) FILTER (WHERE runs > 1 AND ever_graded)                 AS rerun_graded,
    COUNT(*) FILTER (WHERE runs > 1 AND ever_graded AND deposits > 1) AS any_disagreement,
    COUNT(*) FILTER (WHERE runs > 1 AND graded_deposits > 1)          AS contradictions
FROM grouped;
"""


async def envelope_self_contradiction(db, *, tenant_id: Any) -> dict[str, Any]:
    """Count orders the envelope confidently matched two different ways.

    Returns the count, the population it was measured over, and — when there is a
    population — a one-sided 95% upper bound via the rule of three, which is the
    honest way to report zero observed errors. `rate` is None on an empty population,
    never 0.0: "nothing to measure" must not render as "no errors found".
    """
    row = (await db.execute(text(_SQL), {"tenant_id": str(tenant_id)})).fetchone()
    rerun_graded, any_disagreement, contradictions = (int(v or 0) for v in row)

    rate = (contradictions / rerun_graded) if rerun_graded else None
    # Rule of three: with 0 events in n trials the one-sided 95% upper bound is 3/n.
    # Only meaningful for the zero case; above it, report the point estimate and say
    # the interval needs a real method.
    upper_95 = (3.0 / rerun_graded) if (rerun_graded and contradictions == 0) else None

    return {
        "measures": "envelope self-contradiction: one order, two envelope-grade matches to different deposits",
        "estimand": "P(the envelope contradicts itself | order re-reconciled over the same window)",
        "is_lower_bound": True,
        "not_a_correctness_measure": (
            "self-consistency is not correctness — a reliably wrong rule scores zero here; "
            "only a randomised human sample inside the envelope estimates correctness"
        ),
        "population_is_not_random": (
            "which windows get re-run is an operational accident, so this is a diagnostic "
            "over what happened to be re-reconciled, not a population estimate"
        ),
        "rerun_envelope_grade_orders": rerun_graded,
        "deposit_disagreements_any_grade": any_disagreement,
        "contradictions": contradictions,
        "rate": rate,
        "upper_bound_95_if_zero": upper_95,
        "coverage": "100% of re-reconciled orders (no sampling, no human input)",
    }
