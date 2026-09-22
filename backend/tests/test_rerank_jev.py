"""Reranking retrieved passages with Jev: order by usefulness, drop only the clearly useless."""

import uuid

from app.core.config import settings
from app.services.chat import orchestrator
from app.services.chat import rerank_jev as rr
from app.services.typesafe.client import JevResult, JevUnavailableError

TENANT = uuid.uuid4()
CHUNKS = [
    {"content": "Holiday schedule for the office.", "source_name": "HR", "web_view_link": "u1", "similarity": 0.71},
    {
        "content": "Refunds post to account 4100 for EU.",
        "source_name": "Policy",
        "web_view_link": "u2",
        "similarity": 0.66,
    },
    {"content": "Refund approvals need a manager.", "source_name": "SOP", "web_view_link": "u3", "similarity": 0.61},
]


def _scores(*pairs):
    return {
        f"p{i}": {"type": "score", "score": s, "confidence": c, "probabilities": {}, "legend": {}}
        for i, (s, c) in enumerate(pairs)
    }


def _patch(monkeypatch, answers=None, error=None):
    seen = {}

    async def fake_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        if build is not None:
            state, questions = build()
        seen.update(state=state, questions=questions)
        if error:
            return None, error.reason
        return JevResult(answers=answers, model="jev-1.13.0", input_tokens=700, elapsed_ms=140), None

    monkeypatch.setattr(rr, "try_ask", fake_ask)
    return seen


async def test_off_returns_the_passages_untouched_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "off")
    seen = _patch(monkeypatch, answers=_scores((0, 0.9), (2, 0.9), (1, 0.9)))
    kept, record = await rr.rerank(TENANT, "how do EU refunds post?", CHUNKS)
    assert kept == CHUNKS and record is None and seen == {}


async def test_one_request_scores_every_passage(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    seen = _patch(monkeypatch, answers=_scores((0, 0.9), (2, 0.9), (1, 0.9)))
    await rr.rerank(TENANT, "how do EU refunds post?", CHUNKS)
    assert set(seen["questions"]) == {"p0", "p1", "p2"}
    assert seen["state"]["query"] == "how do EU refunds post?"
    assert len(seen["state"]["passages"]) == 3


async def test_live_orders_by_usefulness_and_drops_the_confidently_irrelevant(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    _patch(monkeypatch, answers=_scores((0.05, 0.95), (1.9, 0.9), (1.1, 0.8)))
    kept, record = await rr.rerank(TENANT, "how do EU refunds post?", CHUNKS)
    assert [c["source_name"] for c in kept] == ["Policy", "SOP"]
    assert record["dropped"] == 1 and record["decided_by"] == "jev"


async def test_live_keeps_a_low_score_it_is_not_sure_about(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    _patch(monkeypatch, answers=_scores((0.2, 0.4), (1.9, 0.9), (1.1, 0.8)))
    kept, _ = await rr.rerank(TENANT, "q", CHUNKS)
    assert len(kept) == 3 and kept[-1]["source_name"] == "HR"


async def test_shadow_changes_nothing_and_records_what_it_would_have_done(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "shadow")
    _patch(monkeypatch, answers=_scores((0.05, 0.95), (1.9, 0.9), (1.1, 0.8)))
    kept, record = await rr.rerank(TENANT, "q", CHUNKS)
    assert kept == CHUNKS
    assert record["decided_by"] == "retriever" and record["dropped"] == 1 and record["order_changed"] is True
    assert "Refunds" not in str(record) and "Holiday" not in str(record)


async def test_outage_returns_the_retrievers_order(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    _patch(monkeypatch, error=JevUnavailableError("timeout"))
    kept, record = await rr.rerank(TENANT, "q", CHUNKS)
    assert kept == CHUNKS and record["jev_error"] == "timeout"


async def test_nothing_to_rerank_makes_no_call(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    seen = _patch(monkeypatch, answers={})
    assert await rr.rerank(TENANT, "q", CHUNKS[:1]) == (CHUNKS[:1], None)
    assert seen == {}


async def test_drive_knowledge_uses_the_reranked_chunks_and_rebuilds_sources(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    _patch(monkeypatch, answers=_scores((0.05, 0.95), (1.9, 0.9), (1.1, 0.8)))

    async def fake_retrieve(**_):
        return list(CHUNKS)

    monkeypatch.setattr(orchestrator, "retrieve_drive_chunks", fake_retrieve)
    out = await orchestrator._gather_drive_knowledge(db=None, tenant_id=TENANT, query_text="q")
    assert [c["source_name"] for c in out["chunks"]] == ["Policy", "SOP"]
    assert out["sources"] == {"Policy": "u2", "SOP": "u3"}


async def test_a_passage_jev_only_saw_part_of_is_never_dropped(monkeypatch):
    """Codex: judging 1,500 characters and then discarding the whole passage is unsafe.
    A truncated passage may be demoted, never dropped."""
    monkeypatch.setattr(settings, "JEV_RERANK_MODE", "live")
    long_irrelevant = {**CHUNKS[0], "content": "x" * (rr._MAX_PASSAGE_CHARS + 200)}
    _patch(monkeypatch, answers=_scores((0.0, 0.99), (1.9, 0.9), (1.1, 0.8)))
    kept, record = await rr.rerank(TENANT, "q", [long_irrelevant, CHUNKS[1], CHUNKS[2]])
    assert len(kept) == 3 and kept[-1] is long_irrelevant
    assert record["dropped"] == 0 and record["truncated"] == 1
