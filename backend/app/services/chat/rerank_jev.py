"""Rerank retrieved passages with one Jev request.

Vector similarity finds passages that SOUND like the question; it cannot tell
which ones help answer it, and every unhelpful passage in the prompt is a
distraction the model may act on. Jev (services/typesafe/client.py) scores each
passage's usefulness against the question — all passages in one request,
evaluated in parallel.

Dropping context is the risky half, so it is conservative: a passage goes only
when Jev rates it irrelevant AND is confident. Anything it is unsure about stays,
just lower in the order. Any Jev failure returns the retriever's list unchanged.
This is a relevance filter, not a security control — Jev can itself be swayed by
adversarial text.

JEV_RERANK_MODE: off · shadow (nothing changes; what would have changed is
recorded) · live. Records hold scores and counts, never passage text.
"""

from __future__ import annotations

from app.core.config import settings
from app.services.typesafe.client import try_ask

_LEVELS = [
    "Unrelated to the question; it would not help answer it.",
    "On the same topic, but it does not contain what the question needs.",
    "It contains information that directly helps answer the question.",
]
_DROP_BELOW, _DROP_CONFIDENCE = 0.5, 0.8
_MAX_PASSAGE_CHARS = 1500


async def rerank(tenant_id, query: str, passages: list[dict], *, text_key: str = "content"):
    """Return (passages, record). ``record`` is None when nothing was attempted."""
    mode = settings.JEV_RERANK_MODE
    if mode not in {"shadow", "live"} or len(passages) < 2:
        return passages, None

    def build():
        state = {"query": query, "passages": [(p.get(text_key) or "")[:_MAX_PASSAGE_CHARS] for p in passages]}
        questions = {
            f"p{i}": {
                "type": "score",
                "instructions": f"How useful is `passages[{i}]` for answering `query`?",
                "criteria": _LEVELS,
            }
            for i in range(len(passages))
        }
        return state, questions

    record = {"mode": mode, "decided_by": "retriever", "passages": len(passages), "jev_error": None}
    # try_ask cannot raise, and builds the request inside its guard: retrieval must never
    # fail because of the reranker.
    result, reason = await try_ask(tenant_id, build=build)
    if result is None:
        record["jev_error"] = reason
        return passages, record

    scored = [
        (result.answers[f"p{i}"]["score"], result.answers[f"p{i}"]["confidence"], i) for i in range(len(passages))
    ]
    keep = [(s, i) for s, c, i in scored if not (s < _DROP_BELOW and c >= _DROP_CONFIDENCE)]
    order = [i for _, i in sorted(keep, key=lambda pair: (-pair[0], pair[1]))]
    record.update(
        jev_elapsed_ms=result.elapsed_ms,
        jev_input_tokens=result.input_tokens,
        scores=[round(s, 2) for s, _, _ in scored],
        dropped=len(passages) - len(order),
        order_changed=order != list(range(len(passages))),
    )
    if mode == "shadow":
        return passages, record
    record["decided_by"] = "jev"
    return [passages[i] for i in order], record
