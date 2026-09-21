"""The atomic judge: several narrow Jev judgments, combined by weights that live in code.

It exists to make the benchmark's answer score repeatable and to stop rewarding an
answer merely for containing numbers. It runs BESIDE the Haiku judge; it never sets
the score the merge gate reads.
"""

import uuid

import pytest

from app.core.config import settings
from app.services.benchmarks import atomic_judge as aj
from app.services.benchmarks import run_vs_mcp
from app.services.benchmarks.scorer import ScoreResult
from app.services.typesafe.client import JevResult, JevUnavailableError

TENANT = uuid.uuid4()


def _answers(declines=0.02, addresses=2.0, presents=0.95, hedging=0.0, terms=0.95):
    return {
        "declines": {"type": "noul", "noul": declines},
        "addresses": {"type": "score", "score": addresses, "probabilities": {}, "confidence": 0.9, "legend": {}},
        "presents_result": {"type": "noul", "noul": presents},
        "hedging": {"type": "score", "score": hedging, "probabilities": {}, "confidence": 0.9, "legend": {}},
        "terms_in_result": {"type": "noul", "noul": terms},
    }


def _patch(monkeypatch, answers=None, error=None):
    seen = {}

    async def fake_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        if build is not None:
            state, questions = build()
        seen.update(tenant_id=tenant_id, state=state, questions=questions)
        if error:
            return None, error.reason
        return JevResult(answers=answers, model="jev-1.13.0", input_tokens=500, elapsed_ms=120), None

    monkeypatch.setattr(aj, "try_ask", fake_ask)
    return seen


def test_a_direct_complete_answer_scores_high():
    assert aj.combine(_answers(), terms_expected=True) >= 0.9


def test_an_answer_that_declines_is_capped_however_relevant_it_sounds():
    score = aj.combine(_answers(declines=0.9, addresses=2.0, presents=0.8, terms=0.9), terms_expected=True)
    assert score <= 0.2


def test_describing_the_method_without_a_result_scores_low():
    assert aj.combine(_answers(addresses=1.0, presents=0.05, terms=0.1), terms_expected=True) < 0.5


def test_heavy_hedging_costs_points():
    assert aj.combine(_answers(hedging=2.0), terms_expected=True) < aj.combine(_answers(), terms_expected=True)


def test_no_expected_terms_means_that_component_is_not_counted():
    assert aj.combine(_answers(terms=0.0), terms_expected=False) >= 0.9


async def test_empty_answer_scores_zero_without_a_call(monkeypatch):
    seen = _patch(monkeypatch, answers=_answers())
    result = await aj.atomic_judge_score(tenant_id=TENANT, question="q", answer_text="", expected_contains=[])
    assert result.score == 0.0 and seen == {}


async def test_score_carries_its_components_and_asks_no_question_about_numbers(monkeypatch):
    seen = _patch(monkeypatch, answers=_answers())
    result = await aj.atomic_judge_score(
        tenant_id=TENANT,
        question="Sales in Norway?",
        answer_text="Norway sales are shown.",
        expected_contains=["Norway"],
    )
    assert isinstance(result, ScoreResult) and result.source == "atomic_judge"
    assert "declines=" in result.rationale and "addresses=" in result.rationale
    assert seen["tenant_id"] == TENANT
    assert set(seen["questions"]) == {"declines", "addresses", "presents_result", "hedging", "terms_in_result"}
    wording = str(seen["questions"]).lower()
    assert "number" not in wording and "numeric" not in wording and "figure" not in wording


async def test_unavailable_jev_is_reported_not_guessed(monkeypatch):
    _patch(monkeypatch, error=JevUnavailableError("tenant_not_allowed"))
    result = await aj.atomic_judge_score(tenant_id=TENANT, question="q", answer_text="a", expected_contains=[])
    assert result is None


# ── the merge gate's runner: annotate, never decide ────────────────────────


@pytest.fixture
def haiku(monkeypatch):
    async def fake_llm_judge(**_):
        return ScoreResult(score=0.8, rationale="haiku says fine", source="llm_judge")

    monkeypatch.setattr("app.services.benchmarks.scorer.llm_judge_score", fake_llm_judge)


async def test_gate_score_is_untouched_in_shadow_and_the_atomic_score_is_appended(monkeypatch, haiku):
    monkeypatch.setattr(settings, "JEV_JUDGE_MODE", "shadow")
    _patch(monkeypatch, answers=_answers(declines=0.95))
    score, rationale = await run_vs_mcp._score_answer(
        question="q", answer_text="I could not find it", expected_contains=[], use_llm_judge=True, tenant_id=TENANT
    )
    assert score == 0.8
    assert "haiku says fine" in rationale and "atomic=" in rationale


async def test_off_leaves_the_rationale_alone(monkeypatch, haiku):
    monkeypatch.setattr(settings, "JEV_JUDGE_MODE", "off")
    seen = _patch(monkeypatch, answers=_answers())
    score, rationale = await run_vs_mcp._score_answer(
        question="q", answer_text="a", expected_contains=[], use_llm_judge=True, tenant_id=TENANT
    )
    assert score == 0.8 and "atomic" not in rationale and seen == {}


async def test_there_is_no_mode_in_which_jev_sets_the_gate_score(monkeypatch, haiku):
    monkeypatch.setattr(settings, "JEV_JUDGE_MODE", "live")
    _patch(monkeypatch, answers=_answers(declines=0.95))
    score, _ = await run_vs_mcp._score_answer(
        question="q", answer_text="a", expected_contains=[], use_llm_judge=True, tenant_id=TENANT
    )
    assert score == 0.8


async def test_a_jev_failure_never_breaks_scoring(monkeypatch, haiku):
    monkeypatch.setattr(settings, "JEV_JUDGE_MODE", "shadow")
    _patch(monkeypatch, error=JevUnavailableError("timeout"))
    score, rationale = await run_vs_mcp._score_answer(
        question="q", answer_text="a", expected_contains=[], use_llm_judge=True, tenant_id=TENANT
    )
    assert score == 0.8 and "atomic=unavailable" in rationale


async def test_the_judge_is_told_when_a_result_table_was_displayed(monkeypatch):
    seen = _patch(monkeypatch, answers=_answers())
    await aj.atomic_judge_score(
        tenant_id=TENANT, question="q", answer_text="See the table.", expected_contains=[], result_table_displayed=True
    )
    assert seen["state"]["result_table_displayed"] is True
    assert "result_table_displayed" in str(seen["questions"]["presents_result"])


def test_a_table_tool_counts_as_a_displayed_table_and_a_search_does_not():
    assert run_vs_mcp._showed_a_data_table([{"name": "netsuite_suiteql"}]) is True
    assert run_vs_mcp._showed_a_data_table([{"tool": "rag_search"}]) is False
    assert run_vs_mcp._showed_a_data_table([]) is False
