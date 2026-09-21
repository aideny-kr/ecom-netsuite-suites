"""An answer score built from narrow Jev judgments, combined by weights in code.

The Haiku judge (scorer.llm_judge_score) returns one holistic number from a
generative model, and its rubric pays 0.8+ for "real numeric answers" — so an
answer that merely restates figures is rewarded. This judge asks five closed
questions instead, none of them about numbers, and ``combine`` — plain
arithmetic you can read and change — turns them into a score. Whether the
figures are RIGHT is not a judgment call and is not made here; that belongs to
code such as scorer.assert_computed_value_absent.

It is a measurement that runs beside the Haiku judge (JEV_JUDGE_MODE=shadow).
Nothing in this module or its caller lets it set the score the merge gate reads:
changing how the gate scores is a policy decision, not a side effect of a flag.

The judged answer is produced from a tenant's data, so every call goes through
the Jev client's tenant allowlist like any other.
"""

from __future__ import annotations

from app.services.benchmarks.scorer import ScoreResult
from app.services.typesafe.client import JevUnavailableError, ask

# Weights for an answer that does not decline. They sum to 1.0.
W_ADDRESSES, W_PRESENTS, W_TERMS = 0.5, 0.3, 0.2
HEDGING_PENALTY = 0.1  # per hedging level (0 none · 1 some · 2 heavy)
DECLINED_CAP = 0.2  # matches the Haiku rubric's ceiling for "failed to answer"

_QUESTIONS = {
    "declines": {
        "type": "noul",
        "instructions": "Does `answer` say the requested information could not be found, retrieved or produced?",
        "criteria": {
            "true": "It reports no data, a failure, an error, or explains why it could not answer.",
            "false": "It gives the requested information.",
        },
    },
    "addresses": {
        "type": "score",
        "instructions": "How completely does `answer` respond to what `question` asks?",
        "criteria": [
            "It does not respond to what was asked.",
            "It responds to part of what was asked.",
            "It responds to everything that was asked.",
        ],
    },
    "presents_result": {
        "type": "noul",
        "instructions": (
            "Does `answer` deliver a result for `question`? When `result_table_displayed` is true, pointing the "
            "reader to the displayed table counts as delivering the result."
        ),
        "criteria": {
            "true": "It states the result, or directs the reader to a result table that was displayed.",
            "false": "It only describes a method, the data's structure, or what could be done.",
        },
    },
    "hedging": {
        "type": "score",
        "instructions": "How much does `answer` qualify or cast doubt on its own result?",
        "criteria": ["No hedging.", "Some caveats alongside a clear result.", "So hedged that the result is unclear."],
    },
    "terms_in_result": {
        "type": "noul",
        "instructions": "Are the `expected_terms` used as part of the result in `answer`?",
        "criteria": {
            "true": "They appear as part of what the answer reports.",
            "false": "They are absent, or appear only while explaining a failure.",
        },
    },
}


def combine(answers: dict, *, terms_expected: bool) -> float:
    addresses = answers["addresses"]["score"] / 2.0
    presents = answers["presents_result"]["noul"]
    if terms_expected:
        score = W_ADDRESSES * addresses + W_PRESENTS * presents + W_TERMS * answers["terms_in_result"]["noul"]
    else:
        score = (W_ADDRESSES * addresses + W_PRESENTS * presents) / (W_ADDRESSES + W_PRESENTS)
    score -= HEDGING_PENALTY * answers["hedging"]["score"]
    if answers["declines"]["noul"] >= 0.5:
        score = min(score, DECLINED_CAP)
    return round(max(0.0, min(1.0, score)), 3)


async def atomic_judge_score(
    *, tenant_id, question: str, answer_text: str, expected_contains: list[str], result_table_displayed: bool = False
) -> ScoreResult | None:
    """The atomic score, or None when Jev is unavailable — never a guessed number.

    ``result_table_displayed``: the product shows tool-computed numbers in a table and
    tells the model not to repeat them, so a correct answer often says "see the table".
    Without this flag Jev, which reads literally, scores that as delivering nothing.
    """
    if not answer_text:
        return ScoreResult(score=0.0, rationale="empty answer", source="atomic_judge")
    state = {
        "question": question,
        "answer": answer_text[:4000],
        "expected_terms": expected_contains,
        "result_table_displayed": result_table_displayed,
    }
    try:
        answers = (await ask(tenant_id, state, _QUESTIONS)).answers
    except JevUnavailableError:
        return None
    parts = (
        f"declines={answers['declines']['noul']:.2f} addresses={answers['addresses']['score']:.2f}/2 "
        f"presents={answers['presents_result']['noul']:.2f} hedging={answers['hedging']['score']:.2f}/2 "
        f"terms={answers['terms_in_result']['noul']:.2f}"
    )
    return ScoreResult(
        score=combine(answers, terms_expected=bool(expected_contains)), rationale=parts, source="atomic_judge"
    )
