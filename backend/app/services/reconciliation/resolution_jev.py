"""Jev classification of planner abstentions, beside the LLM ResolutionAgent.

Jev (services/typesafe/client.py) picks one of AGENT_ALLOWED_ACTIONS in roughly
a tenth of a second, but its vendor documents it as unreliable with numbers,
dates and accounting periods. So the split is strict: ``derive_facts`` does all
arithmetic in Decimal and names the result ("variance_matches_payout_fee"); Jev
sees those names plus free text with every number folded to <NUM> (``_scrub``) —
no figure leaves, in any field. Its answer then passes
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
from app.services.reconciliation.narrative_contract import _NUM_RE
from app.services.reconciliation.resolution_agent import AGENT_ALLOWED_ACTIONS, validate_output
from app.services.reconciliation.resolution_planner import FEE_EXPLAIN_TOLERANCE
from app.services.typesafe.client import try_ask

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


def _scrub(value):
    """Fold every number token in every string to <NUM>, recursively.

    The contract is "no figure leaves", and a contract enforced by picking which fields to
    filter is only as good as the list: free text such as "Variance of $3.20 matches Stripe
    processing fee (fee_amount=3.20)" sailed past a filter that looked only at evidence
    values. So this runs over the WHOLE outgoing state as the last step of build_request,
    using the recon package's own number tokenizer, and the test asserts that no digit of
    any kind survives. Jev loses nothing it can use: it is unreliable with numbers, and
    every numeric judgment already reaches it as a named fact from derive_facts.
    """
    if isinstance(value, str):
        return _NUM_RE.sub("<NUM>", value)
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def build_request(context: dict) -> tuple[dict, dict]:
    # A value that is nothing but a figure carries no meaning once folded, so drop the key.
    evidence = {k: v for k, v in (context.get("evidence") or {}).items() if not _NUMERIC.match(str(v))}
    state = _scrub(
        {
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
    )
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
        "mode": mode, "jev_action": None, "jev_confidence": None, "jev_probabilities": None,
        "jev_elapsed_ms": None, "jev_error": None, "llm_action": None, "llm_elapsed_ms": None,
        "agree": None, "decided_by": "llm", "guard_veto": None, "applied_action": None,
    }  # fmt: skip

    llm_out = None
    if mode == "shadow":
        # Concurrent: shadow must cost the item no extra time inside its timeout budget.
        jev_fields, (llm_out, record["llm_elapsed_ms"]) = await asyncio.gather(
            _jev(tenant_id, context), _llm(adapter, model, context)
        )
    else:
        jev_fields = await _jev(tenant_id, context)
    record.update(jev_fields)

    confident = record["jev_action"] is not None and record["jev_confidence"] >= settings.JEV_RECON_MIN_CONFIDENCE
    if mode == "live" and confident:
        # Building Jev's proposal is Jev-side work: if it breaks, the item goes to the LLM
        # exactly as if Jev had been unsure. validate_output stays OUTSIDE this guard — a
        # failure there is a failure of the shared safety net and must surface as before.
        try:
            facts = derive_facts(context)
            out = {
                "action": record["jev_action"],
                "narrative": template_narrative(record["jev_action"], facts),
                "key_evidence": [key for key in _BASIS if facts.get(key) is True],
            }
        except Exception as exc:
            record["jev_error"] = f"unexpected:{type(exc).__name__}"
            out = None
        if out is not None:
            validated = validate_output(out, context, materiality)
            record["guard_veto"] = validated.get("contract_violation")
            record["decided_by"] = "guard" if record["guard_veto"] else "jev"
            record["applied_action"] = validated["action"]
            return validated, record

    if llm_out is None:
        llm_out, record["llm_elapsed_ms"] = await _llm(adapter, model, context)
    validated = validate_output(llm_out, context, materiality)
    record["llm_action"] = record["applied_action"] = validated["action"]
    if record["jev_action"] is not None:
        record["agree"] = record["jev_action"] == validated["action"]
    return validated, record
