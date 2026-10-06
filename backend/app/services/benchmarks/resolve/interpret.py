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
    try:
        body = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(body, dict) or body.get("diagnosis") not in DIAGNOSES or body.get("action") not in ACTIONS:
        return None
    return {"diagnosis": body["diagnosis"], "action": body["action"]}


def make_interpreter(*, api_key: str, model: str):
    """An async `interpret(reply_text)` backed by one Anthropic call per reply."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)

    async def interpret(reply_text: str) -> dict | None:
        if not reply_text.strip():
            return None
        response = await client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": PROMPT + reply_text[:8000]}],
        )
        text = "".join(getattr(block, "text", "") for block in response.content)
        return parse_interpretation(text)

    return interpret
