"""A1 — Jev classification of recon planner abstentions.

Jev is unreliable with numbers, so code derives every numeric relationship as a
named fact and Jev only sees facts and text. The decision function keeps the
existing LLM path as the fallback and as the authority in shadow mode.
"""

import uuid
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.reconciliation import resolution_jev as rj
from app.services.reconciliation.narrative_contract import narrative_respects_evidence
from app.services.reconciliation.resolution_agent import AGENT_ALLOWED_ACTIONS, _flatten_values
from app.services.typesafe.client import JevResult, JevUnavailableError

TENANT = uuid.uuid4()
MATERIALITY = (Decimal("10.00"), Decimal("0.01"))


def _context(**over):
    base = {
        "root_cause": "amount_mismatch",
        "planner_action": "needs_human",
        "planner_narrative": "Planner could not explain the difference.",
        "proposed_amount": "3.20",
        "currency": "USD",
        "above_materiality": "False",
        "variance_type": "amount_mismatch",
        "variance_amount": "-3.20",
        "stripe_amount": "100.00",
        "netsuite_amount": "96.80",
        "variance_explanation": "NetSuite deposit is lower than the Stripe charge.",
        "evidence": {"order_reference": "R123456789"},
        "candidate_postings": [
            {
                "record_type": "customerdeposit",
                "amount": "96.80",
                "currency": "USD",
                "memo": "Order R123456789",
                "netsuite_internal_id": "555",
            }
        ],
        "payout_line": {
            "line_type": "charge",
            "amount": "100.00",
            "fee": "3.20",
            "net": "96.80",
            "currency": "USD",
            "description": "Charge for R123456789",
        },
    }
    base.update(over)
    return base


def _jev(action, confidence):
    probabilities = {a: 0.0 for a in AGENT_ALLOWED_ACTIONS}
    probabilities[action] = 1.0
    answers = {"action": {"type": "choice", "choice": action, "probabilities": probabilities, "confidence": confidence}}
    return JevResult(answers=answers, model="jev-1.13.0", input_tokens=400, elapsed_ms=90)


class _Adapter:
    """Stands in for the LLM path; records whether it was used."""

    def __init__(self, action="book_fee_line"):
        self.calls = 0
        self.action = action


@pytest.fixture
def llm(monkeypatch):
    adapter = _Adapter()

    async def fake_classify(adapter_, model, context):
        adapter_.calls += 1
        return {"action": adapter_.action, "narrative": "Fee explains the difference.", "key_evidence": ["fee"]}

    monkeypatch.setattr(rj.resolution_agent, "classify_item", fake_classify)
    return adapter


def _patch_jev(monkeypatch, result=None, error=None):
    seen = {}

    async def fake_ask(tenant_id, state, questions, **_):
        seen["state"], seen["questions"], seen["tenant_id"] = state, questions, tenant_id
        if error:
            raise error
        return result

    monkeypatch.setattr(rj, "ask", fake_ask)
    return seen


# ── facts: arithmetic stays in code ────────────────────────────────────────


def test_fee_explained_variance_is_a_named_fact():
    facts = rj.derive_facts(_context())
    assert facts["variance_matches_payout_fee"] is True
    assert facts["netsuite_lower_than_stripe"] is True
    assert facts["candidate_memo_mentions_order_reference"] is True
    assert facts["candidate_count"] == "one"


def test_unrelated_variance_does_not_match_the_fee():
    facts = rj.derive_facts(_context(variance_amount="-41.00", netsuite_amount="59.00"))
    assert facts["variance_matches_payout_fee"] is False


def test_missing_amounts_yield_unknown_not_a_guess():
    facts = rj.derive_facts(_context(stripe_amount=None, netsuite_amount=None, payout_line=None, candidate_postings=[]))
    assert facts["netsuite_lower_than_stripe"] is None
    assert facts["variance_matches_payout_fee"] is None
    assert facts["candidate_count"] == "none"


def test_request_carries_facts_and_text_but_no_raw_amounts():
    state, questions = rj.build_request(_context())
    flat = str(state)
    for amount in ("100.00", "96.80", "3.20"):
        assert amount not in flat
    assert state["facts"]["variance_matches_payout_fee"] is True
    assert set(questions["action"]["criteria"]) == set(AGENT_ALLOWED_ACTIONS)
    assert questions["action"]["type"] == "choice"


@pytest.mark.parametrize("action", sorted(AGENT_ALLOWED_ACTIONS))
def test_template_narrative_always_satisfies_the_no_invented_numbers_contract(action):
    context = _context()
    narrative = rj.template_narrative(action, rj.derive_facts(context))
    assert narrative
    assert narrative_respects_evidence(narrative, _flatten_values(context))


# ── decision: off / shadow / live ──────────────────────────────────────────


async def test_off_uses_only_the_llm(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")
    seen = _patch_jev(monkeypatch, result=_jev("carry_forward", 0.99))
    validated, shadow = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line"
    assert shadow is None and seen == {} and llm.calls == 1


async def test_shadow_keeps_the_llm_decision_and_records_both(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    _patch_jev(monkeypatch, result=_jev("carry_forward", 0.91))
    validated, shadow = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line"
    assert shadow["mode"] == "shadow"
    assert shadow["llm_action"] == "book_fee_line" and shadow["jev_action"] == "carry_forward"
    assert shadow["agree"] is False and shadow["jev_confidence"] == 0.91
    assert shadow["jev_elapsed_ms"] == 90 and shadow["llm_elapsed_ms"] >= 0
    assert shadow["decided_by"] == "llm"


async def test_shadow_survives_a_jev_failure(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    _patch_jev(monkeypatch, error=JevUnavailableError("timeout"))
    validated, shadow = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line"
    assert shadow["jev_error"] == "timeout" and shadow["jev_action"] is None


async def test_live_confident_jev_decides_without_the_llm(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.95))
    validated, shadow = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line" and "contract_violation" not in validated
    assert llm.calls == 0 and shadow["decided_by"] == "jev"


async def test_live_unsure_jev_falls_back_to_the_llm(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("carry_forward", 0.4))
    validated, shadow = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line"
    assert llm.calls == 1 and shadow["decided_by"] == "llm"


async def test_live_jev_outage_falls_back_to_the_llm(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, error=JevUnavailableError("http_529"))
    validated, shadow = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line" and llm.calls == 1
    assert shadow["jev_error"] == "http_529"


async def test_live_jev_cannot_bypass_the_chargeback_policy(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("writeoff_je", 0.99))
    validated, _ = await rj.decide_item(TENANT, llm, "m", _context(root_cause="chargeback"), MATERIALITY)
    assert validated["action"] == "needs_human"
    assert validated["contract_violation"] == "chargeback_policy"


async def test_live_jev_cannot_write_off_a_material_variance(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("writeoff_je", 0.99))
    context = _context(variance_amount="-41.00", netsuite_amount="59.00")
    validated, _ = await rj.decide_item(TENANT, llm, "m", context, MATERIALITY)
    assert validated["action"] == "needs_human"
    assert validated["contract_violation"] == "writeoff_je above materiality"
