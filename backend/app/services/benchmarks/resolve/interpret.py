"""Read a reply's diagnosis and action when the agent declared none (today's agent).

A model-graded step: one call classifies the reply into the labelling page's vocabularies
and nothing else. It sees only the reply, never the gold label. An answer outside the
vocabularies counts as no reading, so a vague reply fails G1 instead of being guessed
into a category.
"""

from __future__ import annotations

import json

from app.services.benchmarks.resolve.tasks import ACTIONS, DIAGNOSES

PROMPT = """You classify an accounting agent's final reply about one reconciliation case.
Choose exactly one diagnosis and one action the reply commits to. If it commits to none, use null.

diagnosis (what is going on):
- credited: already right, a credit memo in NetSuite covers it
- explained_other: already right, explained another way
- needs_credit_memo: needs a credit memo, the Solidus adjustment isn't booked
- business_pricing: business order priced differently in NetSuite (expected)
- billing_gap: recurring billing gap
- fix_source: Solidus is wrong, fix it at the source
- netsuite_wrong_other: NetSuite is wrong, another correction
- data_issue: comparison or data problem, not an accounting error
- other

action (what should happen): explain_close, create, update, fix_source, escalate

Return only JSON: {"diagnosis": "<one of the above or null>", "action": "<one of the above or null>"}

Reply:
"""


def parse_interpretation(raw: str) -> dict | None:
    """The reading, or None when the grader read no conclusion (``null``): that one is the
    agent's failure. Anything else unusable is the GRADER's failure and raises, so the trial
    is marked not comparable instead of scored against the agent (review round 6)."""
    try:
        body = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"interpreter returned malformed output: {raw[:80]!r}") from exc
    # One strict shape (round 7): exactly the two fields, both null or both in vocabulary.
    if not isinstance(body, dict) or set(body) != {"diagnosis", "action"}:
        raise ValueError("interpreter output must be exactly {diagnosis, action}")
    diagnosis, action = body["diagnosis"], body["action"]
    if diagnosis is None and action is None:
        return None
    if diagnosis in DIAGNOSES and action in ACTIONS:
        return {"diagnosis": diagnosis, "action": action}
    raise ValueError(f"interpreter answered outside the label vocabulary: {diagnosis!r}, {action!r}")


# Far above any reply the agent shows; a longer one is refused, never cut (review round 5).
MAX_INTERPRET_CHARS = 100_000


def make_interpreter(*, api_key: str, model: str):
    """An async `interpret(reply_text)` backed by one Anthropic call per reply."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)

    async def interpret(reply_text: str) -> dict | None:
        if not reply_text.strip():
            return None
        if len(reply_text) > MAX_INTERPRET_CHARS:
            # Reading only the start could take a superseded conclusion for the final one.
            raise ValueError(f"reply too long to interpret ({len(reply_text)} characters)")
        response = await client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": PROMPT + reply_text}],
        )
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise ValueError("interpreter output was cut off")  # a truncated reading is not a reading
        text = "".join(getattr(block, "text", "") for block in response.content)
        return parse_interpretation(text)

    return interpret
