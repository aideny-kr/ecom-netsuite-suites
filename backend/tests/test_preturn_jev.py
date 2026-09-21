"""A2 + A3 — one Jev request for every pre-turn judgment.

Jev may short-circuit the LLM router only for the easy cases. Anything touching
a user's source choice (negation, aliases, corrections) stays with the LLM,
because Jev reads literally and the router's source rules are not literal.
"""

import uuid

import pytest

from app.core.config import settings
from app.services.chat import preturn_jev as pj
from app.services.chat.llm_adapter import TokenUsage
from app.services.chat.request_routing import RequestRoute, RoutingResult, SourceIntent
from app.services.typesafe.client import JevResult, JevUnavailableError

TENANT = uuid.uuid4()
SOURCES = {"netsuite": "ERP", "metabase": "BI"}
KINDS = ("analytics", "transaction", "operations", "conversation")


def _answers(kind="conversation", kind_conf=0.95, continuation=0.05, source_talk=0.02, importance=0.1, skill="none"):
    return {
        "kind": {
            "type": "choice",
            "choice": kind,
            "probabilities": {k: (1.0 if k == kind else 0.0) for k in KINDS},
            "confidence": kind_conf,
        },
        "continuation": {"type": "noul", "noul": continuation},
        "source_talk": {"type": "noul", "noul": source_talk},
        "importance": {"type": "score", "score": importance, "probabilities": {}, "confidence": 0.9, "legend": {}},
        "skill": {"type": "choice", "choice": skill, "probabilities": {}, "confidence": 0.9},
        "needs_skill": {"type": "noul", "noul": 0.1},
    }


def _patch_jev(monkeypatch, answers=None, error=None):
    seen = {}

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        if build is not None:
            state, questions = build()
        seen.update(state=state, questions=questions)
        if error:
            return None, error.reason
        return JevResult(answers=answers, model="jev-1.13.0", input_tokens=900, elapsed_ms=105), None

    monkeypatch.setattr(pj, "try_ask", fake_try_ask)
    return seen


@pytest.fixture
def llm_router(monkeypatch):
    calls = []

    async def fake_classify(**kwargs):
        calls.append(kwargs)
        return RoutingResult(route=RequestRoute(kind="analytics", continuation=False), usage=TokenUsage())

    monkeypatch.setattr(pj.request_routing, "classify_request", fake_classify)
    return calls


def _call(**over):
    kwargs = dict(
        tenant_id=TENANT, task="thanks, that helps", history=[], adapter=object(), model="m", available_sources=SOURCES
    )
    kwargs.update(over)
    return pj.route_request(**kwargs)


# ── the request ────────────────────────────────────────────────────────────


def test_request_asks_every_preturn_question_at_once_over_one_state():
    state, questions = pj.build_request("show revenue", [{"role": "user", "content": "hi"}], SOURCES)
    assert set(questions) == {"kind", "continuation", "source_talk", "importance", "skill", "needs_skill"}
    assert set(questions["kind"]["criteria"]) == set(KINDS)
    assert "none" in questions["skill"]["criteria"] and len(questions["skill"]["criteria"]) > 5
    assert len(questions["importance"]["criteria"]) == 4
    assert state["current_request"] == "show revenue"
    assert state["available_sources"] == SOURCES


# ── which answers may replace the LLM router ───────────────────────────────


def test_confident_non_analytics_short_circuits():
    route = pj.to_route(_answers(kind="conversation", continuation=0.9), floor=0.8)
    assert route == RequestRoute(kind="conversation", continuation=True)
    assert route.source_intent == SourceIntent()


def test_analytics_without_source_talk_short_circuits():
    route = pj.to_route(_answers(kind="analytics", source_talk=0.03), floor=0.8)
    assert route == RequestRoute(kind="analytics", continuation=False)


@pytest.mark.parametrize(
    "answers",
    [
        _answers(kind="analytics", source_talk=0.6),  # the user is talking about sources: LLM territory
        _answers(kind="operations", kind_conf=0.5),  # unsure which kind
        _answers(kind="transaction", continuation=0.5),  # unsure whether it continues
    ],
)
def test_anything_subtle_is_left_to_the_llm_router(answers):
    assert pj.to_route(answers, floor=0.8) is None


# ── off / shadow / live ────────────────────────────────────────────────────


async def test_off_is_the_llm_router_untouched(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "off")
    seen = _patch_jev(monkeypatch, answers=_answers())
    result, record = await _call()
    assert result.route.kind == "analytics" and record is None
    assert seen == {} and len(llm_router) == 1


async def test_shadow_returns_the_llm_route_and_records_the_comparison(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "shadow")
    _patch_jev(monkeypatch, answers=_answers(kind="conversation"))
    result, record = await _call()
    assert result.route.kind == "analytics" and len(llm_router) == 1
    assert record["decided_by"] == "llm"
    assert record["jev_kind"] == "conversation" and record["llm_kind"] == "analytics"
    assert record["kind_agree"] is False and record["would_short_circuit"] is True
    assert record["jev_elapsed_ms"] == 105 and record["llm_elapsed_ms"] >= 0
    # A3 rides in the same request and is compared with today's regex heuristics.
    assert {"jev_importance", "regex_importance", "jev_skill", "substring_skill"} <= set(record)
    # Decisions and timings only.
    assert "thanks" not in str(record)


async def test_shadow_survives_a_jev_outage(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "shadow")
    _patch_jev(monkeypatch, error=JevUnavailableError("timeout"))
    result, record = await _call()
    assert result.route.kind == "analytics"
    assert record["jev_error"] == "timeout" and record["would_short_circuit"] is False


async def test_live_short_circuit_skips_the_llm_router(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "live")
    _patch_jev(monkeypatch, answers=_answers(kind="conversation", continuation=0.95))
    result, record = await _call()
    assert result.route == RequestRoute(kind="conversation", continuation=True)
    assert llm_router == [] and record["decided_by"] == "jev"
    assert result.usage.input_tokens == 0


async def test_live_subtle_request_falls_back_to_the_llm_router(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "live")
    _patch_jev(monkeypatch, answers=_answers(kind="analytics", source_talk=0.7))
    result, record = await _call(task="not netsuite, use the BI one")
    assert result.route.kind == "analytics" and len(llm_router) == 1
    assert record["decided_by"] == "llm"


async def test_live_outage_falls_back_to_the_llm_router(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "live")
    _patch_jev(monkeypatch, error=JevUnavailableError("http_429"))
    result, record = await _call()
    assert len(llm_router) == 1 and record["jev_error"] == "http_429"


async def test_llm_router_failure_still_propagates(monkeypatch):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "shadow")
    _patch_jev(monkeypatch, answers=_answers())

    async def broken(**_):
        raise RuntimeError("router down")

    monkeypatch.setattr(pj.request_routing, "classify_request", broken)
    with pytest.raises(RuntimeError):
        await _call()


# ── gate round 1: nothing on the Jev side may reach the LLM router ─────────


async def test_shadow_survives_a_bug_in_our_own_request_builder(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "shadow")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(settings, "JEV_TENANT_ALLOWLIST", str(TENANT))

    def broken(*_):
        raise KeyError("bug")

    monkeypatch.setattr(pj, "build_request", broken)
    result, record = await _call()
    assert result.route.kind == "analytics" and len(llm_router) == 1
    assert record["jev_error"] == "unexpected:KeyError"


async def test_live_survives_a_bug_in_our_own_request_builder(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "live")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(settings, "JEV_TENANT_ALLOWLIST", str(TENANT))

    def broken(*_):
        raise KeyError("bug")

    monkeypatch.setattr(pj, "build_request", broken)
    result, record = await _call()
    assert len(llm_router) == 1 and record["decided_by"] == "llm"


async def test_the_recorded_short_circuit_is_the_route_that_was_taken(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "live")
    _patch_jev(monkeypatch, answers=_answers(kind="conversation", continuation=0.95))
    calls = []
    real = pj.to_route
    monkeypatch.setattr(pj, "to_route", lambda *a, **k: calls.append(1) or real(*a, **k))
    _, record = await _call()
    assert record["would_short_circuit"] is True and len(calls) == 1


# ── gate round 2: nothing DERIVED from a Jev answer may fail the turn either ─


async def test_shadow_keeps_the_llm_route_when_the_comparison_itself_breaks(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "shadow")
    _patch_jev(monkeypatch, answers=_answers())

    def broken(*a, **k):
        raise OverflowError("cannot convert float infinity to integer")

    monkeypatch.setattr(pj, "_compare", broken)
    result, record = await _call()
    assert result.route.kind == "analytics" and len(llm_router) == 1
    assert record["decided_by"] == "llm" and record["jev_error"] == "unexpected:OverflowError"


async def test_live_falls_back_when_deriving_the_route_breaks(monkeypatch, llm_router):
    monkeypatch.setattr(settings, "JEV_PRETURN_MODE", "live")
    _patch_jev(monkeypatch, answers=_answers(kind="conversation", continuation=0.95))

    def broken(*a, **k):
        raise ValueError("bug")

    monkeypatch.setattr(pj, "to_route", broken)
    result, record = await _call()
    assert result.route.kind == "analytics" and len(llm_router) == 1
    assert record["decided_by"] == "llm" and record["jev_error"] == "unexpected:ValueError"


@pytest.mark.parametrize("edge", [0.2, 0.8])
def test_the_uncertainty_band_includes_its_own_edges(edge):
    assert pj.to_route(_answers(kind="conversation", continuation=edge), floor=0.8) is None
