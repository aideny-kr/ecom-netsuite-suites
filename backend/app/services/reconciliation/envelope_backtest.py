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

⚠️ "Disagree" is compared PER RUN, as sets. An earlier version counted distinct
envelope-grade deposits across the whole group, which a single CORRECT run satisfies
on its own: a split/partial-capture order legitimately yields 2 charges + 2 deposits,
equal counts, full confidence, explicitly not ambiguous. That version manufactured
contradictions out of correct output, and its "lower bound" was neither a lower nor an
upper bound. It reported zero on production only because no such split order happened
to be in the re-run population.

Ship both. If the back-test ever exceeds the sampled upper bound, the SAMPLE is
broken — a permanent, free alarm on the measurement apparatus itself.

WHAT A ZERO HERE DOES NOT MEAN. Self-consistency is not correctness. A matcher that
is reliably wrong — an ambiguous order reference that always resolves to the same
wrong deposit — scores a perfect zero. That is exactly the specification error only
human sampling catches. The return value therefore carries `is_lower_bound=True` and
`measures` describing the estimand, so no caller can read 0.0 as "the matcher is
correct".

The first production reading (2026-08-09, over 5,168 re-run envelope-grade orders)
found ZERO contradictions. That reading was taken with the flawed pre-per-run SQL
described above — which OVER-counts, so a measured zero still implies a true zero,
but any non-zero reading from that version would have been uninterpretable. It has
not been re-run against the corrected query.

Re-runs are also not a random sample of anything: which windows get re-run is an
operational accident. This is a DIAGNOSTIC over whatever happens to have been
re-reconciled, not an estimate of a population rate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

# Below this, 3/n is not a usable approximation and no bound is reported.
_MIN_N_FOR_RULE_OF_THREE = 30

# Envelope grade, inline in SQL. It mirrors `autonomy_envelope.evaluate`'s five rungs
# (not-terminal, bucket=matches, deterministic, zero variance, amount known) rather
# than importing them, because this is a statement about what was true in a PAST run
# and must not silently change meaning when the envelope widens. Diverge only on
# purpose. An earlier draft cited a `recon_decision` module that does not exist on
# this branch and dropped the terminal-status rung outright.
_SQL = """
WITH graded AS (
    SELECT r.date_from,
           r.date_to,
           -- Runs scoped to different subsidiaries are asking DIFFERENT questions:
           -- _fetch_charges/_fetch_deposits filter on subsidiary_id, so a scoped run
           -- legitimately sees a different deposit set than an unscoped one. Comparing
           -- them manufactures contradictions out of correct answers.
           COALESCE(r.subsidiary_id::text, '*') AS scope,
           x.run_id,
           x.evidence->>'order_reference' AS oref,
           x.deposit_id,
           (x.match_type = 'deterministic'
            AND x.bucket = 'matches'
            AND x.variance_amount = 0
            AND x.stripe_amount IS NOT NULL
            AND x.status NOT IN ('approved', 'rejected', 'locked', 'carried_forward')
           ) AS envelope_grade
    FROM reconciliation_results x
    JOIN reconciliation_runs r ON r.id = x.run_id AND r.tenant_id = x.tenant_id
    WHERE x.tenant_id = :tenant_id
      -- Results survive a failed run: _store_results commits BEFORE run.status is set,
      -- and the error path then writes status='failed'. Partial output from a crashed
      -- run is not an envelope claim.
      AND r.status = 'completed'
      AND x.evidence->>'order_reference' IS NOT NULL
      AND x.deposit_id IS NOT NULL
),
-- Per RUN, the SET of deposits that run confidently paired to this order. Comparing
-- SETS rather than a flat distinct-count is the whole correction: one run may
-- legitimately emit several envelope-grade rows for one reference (a split order —
-- 2 charges + 2 deposits, equal counts, full confidence, explicitly NOT ambiguous).
-- Counting deposits across the group charged that single correct run with a
-- contradiction it never made.
per_run AS (
    SELECT date_from, date_to, scope, oref, run_id,
           ARRAY_AGG(DISTINCT deposit_id ORDER BY deposit_id)
               FILTER (WHERE envelope_grade) AS graded_set,
           ARRAY_AGG(DISTINCT deposit_id) AS any_grade_set
    FROM graded
    GROUP BY date_from, date_to, scope, oref, run_id
),
grouped AS (
    SELECT date_from, date_to, scope, oref,
           -- runs that actually staked an envelope claim on this order
           COUNT(*) FILTER (WHERE graded_set IS NOT NULL) AS graded_runs,
           -- distinct ANSWERS among those claims; >1 means two confident runs
           -- named different deposit sets, so at least one was wrong
           COUNT(DISTINCT graded_set) FILTER (WHERE graded_set IS NOT NULL) AS distinct_answers,
           -- distinct deposits named by ANY run, graded or not. SUM(rows) was wrong:
           -- two runs each naming the SAME single deposit summed to 2 and read as a
           -- disagreement, i.e. it counted agreement.
           COUNT(DISTINCT d) AS distinct_deposits_any_grade
    FROM per_run, LATERAL UNNEST(any_grade_set) AS d
    GROUP BY date_from, date_to, scope, oref
)
SELECT
    -- DENOMINATOR: orders where a contradiction was even POSSIBLE — at least two runs
    -- both claimed envelope grade. Counting orders with a single graded run inflates n
    -- and tightens the bound below what the evidence supports.
    COUNT(*) FILTER (WHERE graded_runs > 1)                          AS rerun_graded,
    COUNT(*) FILTER (WHERE graded_runs > 1 AND distinct_answers > 1) AS contradictions,
    COUNT(*) FILTER (WHERE distinct_deposits_any_grade > 1)           AS any_disagreement
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
    # By NAME, never position: reordering the SELECT list once silently swapped
    # contradictions with any_disagreement, and both are plausible small integers,
    # so the result looked entirely reasonable while being wrong.
    rerun_graded = int(row.rerun_graded or 0)
    contradictions = int(row.contradictions or 0)
    any_disagreement = int(row.any_disagreement or 0)

    rate = (contradictions / rerun_graded) if rerun_graded else None
    # Rule of three is a large-n approximation (conventionally n >= 30) and its output
    # is a PROBABILITY. Unclamped it returned 3.0 ("a 300% upper bound") at n=1 — in a
    # module whose whole premise is that no caller may over-read the number, that was
    # the one output that rendered as nonsense.
    upper_95 = (
        min(1.0, 3.0 / rerun_graded) if (rerun_graded >= _MIN_N_FOR_RULE_OF_THREE and contradictions == 0) else None
    )

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
