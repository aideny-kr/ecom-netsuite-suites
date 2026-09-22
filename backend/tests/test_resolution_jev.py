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

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        if build is not None:
            state, questions = build()
        seen["state"], seen["questions"], seen["tenant_id"] = state, questions, tenant_id
        if error:
            return None, error.reason
        return result, None

    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
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


# ── gate round 1 ───────────────────────────────────────────────────────────


async def test_a_guard_veto_is_recorded_as_the_guards_decision_not_jevs(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("writeoff_je", 0.99))
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(root_cause="chargeback"), MATERIALITY)
    assert validated["action"] == "needs_human"
    assert record["decided_by"] == "guard" and record["guard_veto"] == "chargeback_policy"
    assert record["jev_action"] == "writeoff_je" and record["applied_action"] == "needs_human"


async def test_an_unvetoed_jev_decision_records_what_was_applied(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.95))
    _, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert record["decided_by"] == "jev" and record["guard_veto"] is None
    assert record["applied_action"] == "book_fee_line"


async def test_shadow_runs_jev_and_the_llm_concurrently(monkeypatch, llm):
    import asyncio

    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    started = []

    async def slow_try_ask(*a, **k):
        started.append("jev")
        await asyncio.sleep(0.05)
        return _jev("carry_forward", 0.9), None

    async def slow_classify(adapter_, model, context):
        started.append("llm")
        await asyncio.sleep(0.05)
        assert started == ["jev", "llm"], "the LLM call must start before Jev finishes"
        return {"action": "book_fee_line", "narrative": "Fee explains it.", "key_evidence": []}

    monkeypatch.setattr(rj, "try_ask", slow_try_ask)
    monkeypatch.setattr(rj.resolution_agent, "classify_item", slow_classify)
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line" and record["jev_action"] == "carry_forward"


async def test_a_bug_in_the_request_builder_never_reaches_the_llm_path(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(settings, "JEV_TENANT_ALLOWLIST", str(TENANT))

    def broken(_context):
        raise KeyError("bug")

    monkeypatch.setattr(rj, "build_request", broken)
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line" and record["jev_error"] == "unexpected:KeyError"


async def test_out_of_sync_criteria_disable_jev_instead_of_crashing_the_worker(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    seen = _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.99))
    monkeypatch.setattr(rj, "AGENT_ALLOWED_ACTIONS", frozenset({*rj.AGENT_ALLOWED_ACTIONS, "brand_new_action"}))
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line" and llm.calls == 1
    assert record["jev_error"] == "criteria_out_of_sync" and seen == {}


def test_the_criteria_cover_exactly_the_allowed_actions():
    assert set(rj._CRITERIA) == rj.AGENT_ALLOWED_ACTIONS


# ── gate round 2 ───────────────────────────────────────────────────────────


async def test_live_falls_back_to_the_llm_when_building_jevs_proposal_breaks(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.99))

    def broken(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(rj, "template_narrative", broken)
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "book_fee_line" and llm.calls == 1
    assert record["decided_by"] == "llm" and record["jev_error"] == "unexpected:KeyError"


# ── gate round 3: "Jev never sees an amount" must be true, not claimed ─────

_REAL_FEE_TEXT = (
    "Variance of $3.20 matches Stripe processing fee (fee_amount=3.20). NetSuite may have recorded gross amount."
)


def test_no_digit_of_any_kind_leaves_in_any_field():
    import json

    context = _context(
        variance_explanation=_REAL_FEE_TEXT,
        planner_narrative=f"Stripe processing fee. {_REAL_FEE_TEXT} Charged 1,284.55 on USD1284.55.",
        evidence={"order_reference": "R123456789", "fee_amount": "3.20", "note": "refund of 12.50 pending"},
    )
    state, _ = rj.build_request(context)
    sent = json.dumps(state)
    assert not any(ch.isdigit() for ch in sent), sent
    # the words survive; only the figures are folded away
    assert "matches Stripe processing fee" in state["variance_explanation"]
    assert "<NUM>" in state["variance_explanation"] and "fee_amount" not in state["evidence"]
    # and the numeric judgment still reaches Jev, as a fact computed in code
    assert state["facts"]["variance_matches_payout_fee"] is True


# ── gate round 1 on #285 ───────────────────────────────────────────────────


def test_a_candidate_in_another_currency_never_counts_as_an_amount_match():
    context = _context(
        candidate_postings=[{"record_type": "customerdeposit", "amount": "100.00", "currency": "EUR", "memo": "x"}]
    )
    facts = rj.derive_facts(context)
    assert facts["candidate_with_exact_stripe_amount"] is False
    assert facts["currency_consistent"] is False


def test_a_fee_in_another_currency_cannot_explain_the_variance():
    context = _context(payout_line={**_context()["payout_line"], "currency": "EUR"})
    facts = rj.derive_facts(context)
    assert facts["variance_matches_payout_fee"] is None
    assert facts["currency_consistent"] is False


def test_same_currency_everywhere_is_consistent():
    assert rj.derive_facts(_context())["currency_consistent"] is True


def test_payout_recency_and_washout_are_facts_computed_in_code():
    recent = rj.derive_facts(_context(payout={"status": "in_transit", "days_since_arrival": 2}))
    assert recent["payout_recent"] is True and recent["payout_status"] == "in_transit"
    stale = rj.derive_facts(_context(payout={"status": "paid", "days_since_arrival": 40}))
    assert stale["payout_recent"] is False
    unknown = rj.derive_facts(_context())
    assert unknown["payout_recent"] is None and unknown["payout_status"] is None
    washed = rj.derive_facts(_context(root_cause="washout", evidence={"order_reference": "R1", "washout": "True"}))
    assert washed["washout"] is True
    assert rj.derive_facts(_context())["washout"] is False


def test_carry_forward_criterion_names_only_facts_that_exist():
    text = rj._CRITERIA["carry_forward"]
    for name in ("payout_recent", "washout"):
        assert f"facts.{name}" in text
    assert "recent payout still syncing" not in text


# ── codex cross-examination (2026-09-22) ───────────────────────────────────


def _eur_candidate(foreign="96.80", base="105.10"):
    return {
        "record_type": "customerdeposit", "amount": base, "currency": "USD",
        "transaction_currency": "EUR", "foreign_amount": foreign, "memo": "Order R123456789", "netsuite_internal_id": "9",
    }  # fmt: skip


def test_amounts_compare_in_the_transaction_currency_when_the_posting_carries_one():
    # charge is EUR 96.80; the posting is a EUR 96.80 deposit booked into a USD subsidiary
    line = {**_context()["payout_line"], "currency": "EUR"}
    ctx = _context(currency="EUR", stripe_amount="96.80", payout_line=line, candidate_postings=[_eur_candidate()])
    facts = rj.derive_facts(ctx)
    assert facts["candidate_with_exact_stripe_amount"] is True and facts["currency_consistent"] is True


def test_base_currency_amount_is_never_compared_to_a_foreign_charge():
    ctx = _context(
        currency="EUR", stripe_amount="105.10", candidate_postings=[_eur_candidate(foreign="96.80", base="105.10")]
    )
    assert rj.derive_facts(ctx)["candidate_with_exact_stripe_amount"] is False


def test_search_completeness_is_a_fact_and_only_local_completeness():
    assert rj.derive_facts(_context(candidate_search_complete="True"))["candidate_search_complete"] is True
    assert rj.derive_facts(_context())["candidate_search_complete"] is None


@pytest.mark.parametrize(
    "action, ctx_over, eligible",
    [
        ("book_fee_line", {}, True),
        ("book_fee_line", {"variance_amount": "-41.00", "netsuite_amount": "59.00"}, False),  # fee does not explain it
        ("writeoff_je", {"variance_type": "fx_rounding", "variance_amount": "-0.03", "netsuite_amount": "99.97"}, True),
        ("writeoff_je", {"above_materiality": "True"}, False),
        ("carry_forward", {"variance_type": "timing"}, True),
        (
            "carry_forward",
            {"payout": {"status": "in_transit", "days_since_arrival": "2"}, "variance_type": "missing_in_netsuite"},
            True,
        ),
        (
            "carry_forward",
            {"payout": {"status": "failed", "days_since_arrival": "2"}, "variance_type": "missing_in_netsuite"},
            False,
        ),
        ("carry_forward", {}, False),
        ("apply_deposit", {}, False),  # no applicability evidence exists anywhere in the product today
        ("apply_deposit", {"evidence": {"order_reference": "R1", "deposit_unapplied": "True"}}, True),
        (
            "create_and_apply_deposit",
            {"candidate_search_complete": "True", "candidate_postings": []},
            False,
        ),  # local ≠ source coverage
        ("needs_human", {}, True),
    ],
)
def test_action_eligibility_is_decided_by_facts_in_code(action, ctx_over, eligible):
    facts = rj.derive_facts(_context(**ctx_over))
    assert rj.eligible(action, facts) is eligible, (action, facts)


async def test_live_ineligible_pick_abstains_and_records_the_reason(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("apply_deposit", 0.99))
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "needs_human"
    assert record["decided_by"] == "guard" and record["eligibility_veto"] == "apply_deposit"
    assert llm.calls == 0


async def test_shadow_validates_jevs_pick_too_so_the_veto_rate_is_measurable(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    _patch_jev(monkeypatch, result=_jev("writeoff_je", 0.99))
    context = _context(variance_amount="-41.00", netsuite_amount="59.00")  # above the $10 materiality
    _, record = await rj.decide_item(TENANT, llm, "m", context, MATERIALITY)
    assert record["jev_action"] == "writeoff_je"
    assert record["jev_validated_action"] == "needs_human" and record["jev_veto"] == "writeoff_je above materiality"
    assert record["llm_action"] == "book_fee_line" and record["agree"] is False


async def test_shadow_records_the_item_even_when_the_llm_raises(monkeypatch, llm):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.95))

    async def broken(adapter_, model, context):
        raise TimeoutError("provider down")

    monkeypatch.setattr(rj.resolution_agent, "classify_item", broken)
    validated, record = await rj.decide_item(TENANT, llm, "m", _context(), MATERIALITY)
    assert validated["action"] == "needs_human" and validated["contract_violation"] == "classification_error"
    assert record["llm_error"] == "TimeoutError" and record["jev_action"] == "book_fee_line"
