"""Jev classification of planner abstentions, beside the LLM ResolutionAgent.

Jev (services/typesafe/client.py) picks one of AGENT_ALLOWED_ACTIONS in roughly
a tenth of a second, but its vendor documents it as unreliable with numbers,
dates and accounting periods. So the split is strict: ``derive_facts`` does all
arithmetic in Decimal and names the result ("variance_matches_payout_fee"); Jev
sees only those names plus free text, never an amount. Its answer then passes
through the SAME ``validate_output`` as the LLM's, so the chargeback pin and the
write-off materiality guard hold whichever model decided.

Jev writes no prose. A Jev-decided proposal gets ``template_narrative`` — built
from fact names, containing no digits — which satisfies the no-invented-numbers
contract by construction rather than by detection.

JEV_RECON_RESOLUTION_MODE: off (LLM only) · shadow (LLM decides, Jev recorded
beside it) · live (Jev decides at or above JEV_RECON_MIN_CONFIDENCE, else LLM).
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation

from app.core.config import settings
from app.services.reconciliation.resolution_agent import AGENT_ALLOWED_ACTIONS, classify_item, validate_output
from app.services.reconciliation.resolution_planner import FEE_EXPLAIN_TOLERANCE
from app.services.typesafe.client import JevUnavailableError, ask

_NUMERIC = re.compile(r"^[\s$€£-]*\d[\d,]*(\.\d+)?\s*$")

_CRITERIA = {
    "book_fee_line": (
        "The difference is the payment processor's fee. `facts.variance_matches_payout_fee` is true "
        "and `facts.netsuite_lower_than_stripe` is true."
    ),
    "apply_deposit": (
        "A customer deposit for this order already exists in NetSuite among `candidate_postings` but has "
        "not been applied. Nothing new needs to be created."
    ),
    "create_and_apply_deposit": (
        "The charge settled and `facts.has_order_reference` is true, but no posting in `candidate_postings` "
        "corresponds to it, so a deposit must be created and applied."
    ),
    "writeoff_je": (
        "A small rounding or currency-conversion difference with no other explanation, and "
        "`facts.above_materiality` is false."
    ),
    "carry_forward": (
        "A timing item that needs no booking: a recent payout still syncing, or refunds that cancel the charge out."
    ),
    "needs_human": (
        "None of the other options clearly applies, the evidence conflicts, funds never settled, or a "
        "chargeback, dispute or refund is involved."
    ),
}
assert set(_CRITERIA) == AGENT_ALLOWED_ACTIONS

_BASIS = {
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
        return Decimal(str(value))
    except InvalidOperation:
        return None


def derive_facts(context: dict) -> dict:
    """Every numeric judgment, computed here. ``None`` means unknown, never guessed."""
    variance = _dec(context.get("variance_amount"))
    stripe = _dec(context.get("stripe_amount"))
    netsuite = _dec(context.get("netsuite_amount"))
    fee = _dec((context.get("payout_line") or {}).get("fee"))
    postings = context.get("candidate_postings") or []
    order_reference = (context.get("evidence") or {}).get("order_reference") or ""

    return {
        "root_cause": context.get("root_cause"),
        "variance_type": context.get("variance_type"),
        "above_materiality": str(context.get("above_materiality")) == "True",
        "has_order_reference": bool(order_reference),
        "variance_matches_payout_fee": (
            None if variance is None or fee is None else abs(abs(variance) - abs(fee)) <= FEE_EXPLAIN_TOLERANCE
        ),
        "netsuite_lower_than_stripe": None if stripe is None or netsuite is None else netsuite < stripe,
        "candidate_count": "none" if not postings else "one" if len(postings) == 1 else "several",
        "candidate_with_exact_stripe_amount": (
            None if stripe is None else any(_dec(p.get("amount")) == stripe for p in postings)
        ),
        "candidate_memo_mentions_order_reference": bool(order_reference)
        and any(order_reference.lower() in (p.get("memo") or "").lower() for p in postings),
        "payout_line_type": (context.get("payout_line") or {}).get("line_type"),
    }


def build_request(context: dict) -> tuple[dict, dict]:
    evidence = {k: v for k, v in (context.get("evidence") or {}).items() if not _NUMERIC.match(str(v))}
    state = {
        "facts": derive_facts(context),
        "planner_narrative": context.get("planner_narrative") or "",
        "variance_explanation": context.get("variance_explanation") or "",
        "evidence": evidence,
        "candidate_postings": [
            {"record_type": p.get("record_type"), "memo": p.get("memo") or ""}
            for p in context.get("candidate_postings") or []
        ],
        "payout_line_description": (context.get("payout_line") or {}).get("description") or "",
    }
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


def template_narrative(action: str, facts: dict) -> str:
    basis = [text for key, text in _BASIS.items() if facts.get(key) is True]
    reason = "; ".join(basis) if basis else "no single fact was decisive"
    return f"Decision model proposed {action}. Basis: {reason}. Proposal for human review only."


async def decide_item(tenant_id, adapter, model: str, context: dict, materiality) -> tuple[dict, dict | None]:
    """Return (validated proposal, shadow record or None). Never raises for Jev's sake."""
    mode = settings.JEV_RECON_RESOLUTION_MODE
    if mode not in {"shadow", "live"}:
        return validate_output(await classify_item(adapter, model, context), context, materiality), None

    record = {
        "mode": mode,
        "jev_action": None,
        "jev_confidence": None,
        "jev_probabilities": None,
        "jev_elapsed_ms": None,
        "jev_error": None,
        "llm_action": None,
        "llm_elapsed_ms": None,
        "agree": None,
        "decided_by": "llm",
    }
    facts = derive_facts(context)
    try:
        result = await ask(tenant_id, *build_request(context))
        answer = result.answers["action"]
        record.update(
            jev_action=answer["choice"],
            jev_confidence=answer.get("confidence"),
            jev_probabilities=answer.get("probabilities"),
            jev_elapsed_ms=result.elapsed_ms,
            jev_model=result.model,
        )
    except JevUnavailableError as exc:
        record["jev_error"] = exc.reason

    confident = (
        record["jev_action"] is not None and (record["jev_confidence"] or 0) >= settings.JEV_RECON_MIN_CONFIDENCE
    )
    if mode == "live" and confident:
        record["decided_by"] = "jev"
        out = {
            "action": record["jev_action"],
            "narrative": template_narrative(record["jev_action"], facts),
            "key_evidence": [key for key in _BASIS if facts.get(key) is True],
        }
        return validate_output(out, context, materiality), record

    start = time.monotonic()
    out = await classify_item(adapter, model, context)
    record["llm_elapsed_ms"] = int((time.monotonic() - start) * 1000)
    validated = validate_output(out, context, materiality)
    record["llm_action"] = validated["action"]
    if record["jev_action"] is not None:
        record["agree"] = record["jev_action"] == validated["action"]
    return validated, record
