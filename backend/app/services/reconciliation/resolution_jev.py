"""Jev classification of planner abstentions, beside the LLM ResolutionAgent.

Jev (services/typesafe/client.py) picks one of AGENT_ALLOWED_ACTIONS in roughly
a tenth of a second, but its vendor documents it as unreliable with numbers,
dates and accounting periods. So the split is strict: ``derive_facts`` does all
arithmetic in Decimal and names the result ("variance_matches_payout_fee"). Jev is
an external API, so it is sent those facts and nothing else (``_outgoing_facts``):
no memo, narrative, description, evidence value or identifier, and no figure. Its
answer then passes
through the SAME ``validate_output`` as the LLM's, so the chargeback pin and the
write-off materiality guard hold whichever model decided.

Jev writes no prose. A Jev-decided proposal gets ``template_narrative`` — built
from fact names, containing no digits — which satisfies the no-invented-numbers
contract by construction rather than by detection.

JEV_RECON_RESOLUTION_MODE: off (LLM only) · shadow (LLM decides, Jev recorded
beside it) · live (Jev decides at or above JEV_RECON_MIN_CONFIDENCE, else LLM).
"""

from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal, InvalidOperation

from app.core.config import settings
from app.services.reconciliation import resolution_agent
from app.services.reconciliation.resolution_agent import AGENT_ALLOWED_ACTIONS, validate_output
from app.services.reconciliation.resolution_planner import (
    FEE_EXPLAIN_TOLERANCE,
    RECENCY_HOLD_ROOT_CAUSES,
    RECENT_PAYOUT_LAG_DAYS,
)
from app.services.typesafe.client import try_ask

# A fact leaves as a boolean, null, or a plain lowercase token such as "missing_in_netsuite".
# Anything else (a name, an email, an id with digits) is sent as "other".
_TOKEN = re.compile(r"^[a-z_]{1,40}$")

_CRITERIA = {
    "book_fee_line": (
        "The difference is the payment processor's fee. `facts.variance_matches_payout_fee` is true "
        "and `facts.netsuite_lower_than_stripe` is true."
    ),
    "apply_deposit": (
        "A customer deposit for this order already exists in NetSuite but has not been applied: "
        "`facts.deposit_unapplied_evidence` is true and `facts.candidate_memo_mentions_order_reference` is "
        "true. Nothing new needs to be created."
    ),
    "create_and_apply_deposit": (
        "The charge settled and `facts.has_order_reference` is true, but `facts.candidate_count` is none and "
        "`facts.candidate_search_complete` is true, so a deposit must be created and applied."
    ),
    "writeoff_je": (
        "A small rounding or currency-conversion difference with no other explanation, and "
        "`facts.above_materiality` is false."
    ),
    "carry_forward": (
        "A timing item that needs no booking: `facts.variance_type` is timing (amounts agree, dates "
        "differ), or `facts.washout` is true (same-order refunds cancel the charge out), or the charge is "
        "missing from NetSuite while `facts.payout_recent` is true and `facts.payout_status` is a healthy "
        "status such as paid, pending or in_transit."
    ),
    "needs_human": (
        "None of the other options clearly applies, the evidence conflicts, `facts.currency_consistent` is "
        "false, `facts.payout_status` is failed or canceled (funds never settled), or a chargeback, dispute "
        "or refund is involved."
    ),
}

_BASIS = {
    "washout": "same-order refunds cancel the charge out",
    "payout_recent": "the payout is recent",
    "variance_matches_payout_fee": "the variance matches the payout fee",
    "netsuite_lower_than_stripe": "NetSuite is lower than Stripe",
    "candidate_with_exact_stripe_amount": "a candidate posting carries the exact Stripe amount",
    "candidate_memo_mentions_order_reference": "a candidate posting's memo names the order",
    "has_order_reference": "the order reference is known",
}


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    # NaN / Infinity parse fine and then RAISE on comparison; treat them as unknown.
    return number if number.is_finite() else None


def derive_facts(context: dict) -> dict:
    """Every numeric, date and currency judgment, computed here. ``None`` = unknown, never guessed.

    Amounts are only ever compared within ONE currency: the matching engine refuses a
    cross-currency pair, and so does this. A EUR posting whose nominal amount equals
    the USD charge is not a match, however the digits look.
    """
    variance = _dec(context.get("variance_amount"))
    stripe = _dec(context.get("stripe_amount"))
    netsuite = _dec(context.get("netsuite_amount"))
    currency = context.get("currency")
    line = context.get("payout_line") or {}
    fee = _dec(line.get("fee"))
    fee_same_currency = line.get("currency") in (None, currency)
    postings = context.get("candidate_postings") or []

    def _comparable_amount(p: dict) -> Decimal | None:
        """The posting amount in the CHARGE's currency, or None when no such value exists."""
        if p.get("transaction_currency"):
            return _dec(p.get("foreign_amount")) if p.get("transaction_currency") == currency else None
        return _dec(p.get("amount")) if p.get("currency") in (None, currency) else None

    comparable = [_comparable_amount(p) for p in postings]
    same_currency_postings = [p for p, a in zip(postings, comparable, strict=True) if a is not None]
    order_reference = (context.get("evidence") or {}).get("order_reference") or ""
    payout = context.get("payout") or {}
    days = _dec(payout.get("days_since_arrival"))
    evidence = context.get("evidence") or {}

    return {
        "root_cause": context.get("root_cause"),
        "variance_type": context.get("variance_type"),
        "above_materiality": str(context.get("above_materiality")) == "True",
        "has_order_reference": bool(order_reference),
        "currency_consistent": fee_same_currency and len(same_currency_postings) == len(postings),
        # A zero fee explains nothing, however small the variance (planner rule 7b: fee > 0).
        "variance_matches_payout_fee": (
            None
            if variance is None or fee is None or not fee_same_currency
            else fee > 0 and abs(abs(variance) - abs(fee)) <= FEE_EXPLAIN_TOLERANCE
        ),
        "netsuite_lower_than_stripe": None if stripe is None or netsuite is None else netsuite < stripe,
        "candidate_count": "none" if not postings else "one" if len(postings) == 1 else "several",
        "candidate_with_exact_stripe_amount": (
            None if stripe is None else any(a == stripe for a in comparable if a is not None)
        ),
        "candidate_search_complete": (
            None
            if context.get("candidate_search_complete") is None
            else str(context["candidate_search_complete"]) == "True"
        ),
        "deposit_unapplied_evidence": str(evidence.get("deposit_unapplied")) == "True",
        "candidate_memo_mentions_order_reference": bool(order_reference)
        and any(order_reference.lower() in (p.get("memo") or "").lower() for p in postings),
        "payout_line_type": line.get("line_type"),
        # The planner's own recency rule (RECENT_PAYOUT_LAG_DAYS) and washout evidence,
        # so the carry_forward criterion rests on facts rather than on dates Jev cannot read.
        "payout_status": payout.get("status"),
        "payout_recent": None if days is None else days <= RECENT_PAYOUT_LAG_DAYS,
        "washout": context.get("root_cause") == "washout" or str(evidence.get("washout")) == "True",
    }


def _outgoing_facts(facts: dict) -> dict:
    """The facts as they may leave for the external API: booleans, nulls and plain
    tokens only. Everything Jev needs is already a named fact computed in code, so
    free text is not sent at all rather than filtered: a filter is only as good as its
    list, and a scrub for digits let names, emails and ids through."""

    def safe(value):
        if value is None or isinstance(value, bool):
            return value
        return value if isinstance(value, str) and _TOKEN.match(value) else "other"

    return {name: safe(value) for name, value in facts.items()}


def build_request(context: dict) -> tuple[dict, dict]:
    state = {"facts": _outgoing_facts(derive_facts(context))}
    questions = {
        "action": {
            "type": "choice",
            "instructions": (
                "A reconciliation rule engine could not resolve this exception between a Stripe charge and "
                "NetSuite. Using `facts` as established truth, which resolution fits?"
            ),
            "criteria": _CRITERIA,
        }
    }
    return state, questions


_HEALTHY_PAYOUT = {"paid", "pending", "in_transit"}


def eligible(action: str, facts: dict) -> bool:
    """Facts are not advice Jev may ignore: an action is only acceptable when the facts
    that justify it hold. Absence of evidence never qualifies: apply_deposit needs the
    planner's own unapplied-deposit evidence (which no producer writes today, so Jev
    abstains), and create_and_apply_deposit needs verified SOURCE coverage, which local
    completeness cannot establish — so it is never Jev-eligible until such a fact exists.
    """
    if action == "needs_human":
        return True
    if action == "book_fee_line":
        return bool(
            facts["variance_matches_payout_fee"]
            and facts["netsuite_lower_than_stripe"]
            and facts["currency_consistent"]
        )
    if action == "writeoff_je":
        return not facts["above_materiality"] and facts["variance_type"] in {"fx_rounding", "amount_mismatch"}
    if action == "carry_forward":
        # The recency hold is the planner's rule 7 and applies to MISSING counterparts only;
        # a real amount mismatch on a recent payout is not a timing item.
        missing = facts["variance_type"] in RECENCY_HOLD_ROOT_CAUSES or facts["root_cause"] in RECENCY_HOLD_ROOT_CAUSES
        return bool(
            facts["washout"]
            or facts["variance_type"] == "timing"
            or (missing and facts["payout_recent"] and facts["payout_status"] in _HEALTHY_PAYOUT)
        )
    if action == "apply_deposit":
        return bool(facts["deposit_unapplied_evidence"] and facts["currency_consistent"])
    return False  # create_and_apply_deposit, and anything unknown


def template_narrative(action: str, facts: dict) -> str:
    basis = [text for key, text in _BASIS.items() if facts.get(key) is True]
    reason = "; ".join(basis) if basis else "no single fact was decisive"
    return f"Decision model proposed {action}. Basis: {reason}. Proposal for human review only."


async def _jev(tenant_id, context: dict) -> dict:
    """Jev's reading as record fields. Cannot raise (try_ask), so the LLM path beside it is safe.

    The criteria/allow-list check is a RUNTIME refusal, not an import-time assert: the worker
    imports this module unconditionally, and an assert here would take the whole resolution
    agent down the day someone adds an action — including for tenants with Jev off.
    """
    if set(_CRITERIA) != AGENT_ALLOWED_ACTIONS:
        return {"jev_error": "criteria_out_of_sync"}
    result, reason = await try_ask(tenant_id, build=lambda: build_request(context))
    if result is None:
        return {"jev_error": reason}
    answer = result.answers["action"]
    return {
        "jev_action": answer["choice"],
        "jev_confidence": answer["confidence"],
        "jev_probabilities": answer["probabilities"],
        "jev_elapsed_ms": result.elapsed_ms,
        "jev_model": result.model,
    }


async def _llm(adapter, model: str, context: dict) -> tuple[dict, int]:
    start = time.monotonic()
    out = await resolution_agent.classify_item(adapter, model, context)
    return out, int((time.monotonic() - start) * 1000)


async def decide_item(tenant_id, adapter, model: str, context: dict, materiality) -> tuple[dict, dict | None]:
    """Return (validated proposal, comparison record or None).

    ``decided_by`` names who actually determined the applied action: "jev", "llm", or
    "guard" when validate_output vetoed Jev's pick — so the record can be used to measure
    Jev honestly, including how often the safety net had to step in.
    """
    mode = settings.JEV_RECON_RESOLUTION_MODE
    if mode not in {"shadow", "live"}:
        out = await resolution_agent.classify_item(adapter, model, context)
        return validate_output(out, context, materiality), None

    record = {
        "mode": mode, "jev_action": None, "jev_confidence": None, "jev_probabilities": None, "jev_elapsed_ms": None,
        "jev_error": None, "jev_validated_action": None, "jev_veto": None, "eligibility_veto": None,
        "llm_action": None, "llm_elapsed_ms": None, "llm_error": None, "agree": None, "decided_by": "llm",
        "guard_veto": None, "applied_action": None,
    }  # fmt: skip
    # Deriving facts is Jev-side work: if the context is malformed, Jev is skipped and the
    # LLM path runs untouched, exactly as for any other Jev-side failure.
    try:
        facts = derive_facts(context)
    except Exception as exc:
        facts = None
        record["jev_error"] = f"unexpected:{type(exc).__name__}"
    jev_validated: dict | None = None

    def _jev_proposal(action: str) -> tuple[dict, str | None]:
        """Jev's pick as a validated proposal, plus the veto that stopped it (or None)."""
        if not eligible(action, facts):
            out = {
                "action": "needs_human",
                "narrative": template_narrative("needs_human", facts),
                "key_evidence": [],
            }
            validated = validate_output(out, context, materiality)  # the one validator, for every path
            validated["contract_violation"] = f"jev_ineligible:{action}"
            return validated, action
        out = {
            "action": action,
            "narrative": template_narrative(action, facts),
            "key_evidence": [key for key in _BASIS if facts.get(key) is True],
        }
        validated = validate_output(out, context, materiality)
        return validated, None

    async def _llm_guarded():
        try:
            return await _llm(adapter, model, context), None
        except Exception as exc:  # the primary path failed; record it, never lose the item
            return None, type(exc).__name__

    if facts is None:
        jev_fields = {}
        llm_result, llm_error = (await _llm_guarded()) if mode == "shadow" else (None, None)
    elif mode == "shadow":
        jev_fields, (llm_result, llm_error) = await asyncio.gather(_jev(tenant_id, context), _llm_guarded())
    else:
        jev_fields, llm_result, llm_error = await _jev(tenant_id, context), None, None
    record.update(jev_fields)

    if record["jev_action"] is not None:
        try:
            jev_validated, ineligible = _jev_proposal(record["jev_action"])
            record["eligibility_veto"] = ineligible
            record["jev_veto"] = ineligible or jev_validated.get("contract_violation")
            record["jev_validated_action"] = jev_validated["action"]
        except Exception as exc:
            record["jev_error"] = f"unexpected:{type(exc).__name__}"
            record["jev_action"] = None

    confident = record["jev_action"] is not None and record["jev_confidence"] >= settings.JEV_RECON_MIN_CONFIDENCE
    if mode == "live" and confident and jev_validated is not None:
        record["guard_veto"] = record["jev_veto"]
        record["decided_by"] = "guard" if record["jev_veto"] else "jev"
        record["applied_action"] = jev_validated["action"]
        return jev_validated, record

    if llm_result is None and llm_error is None:
        llm_result, llm_error = await _llm_guarded()
    if llm_result is None:
        record["llm_error"] = llm_error
        validated = {
            "action": "needs_human",
            "narrative": "Agent classification failed; needs investigation.",
            "key_evidence": [],
            "contract_violation": "classification_error",
        }
    else:
        llm_out, record["llm_elapsed_ms"] = llm_result
        validated = validate_output(llm_out, context, materiality)
    record["llm_action"] = record["applied_action"] = validated["action"]
    if record["jev_action"] is not None:
        record["agree"] = record["jev_validated_action"] == validated["action"]
    return validated, record
