"""Code graders for one benchmark trial: the outcome against gold, safety, brevity and cost.

G1 (outcome) passes only when the diagnosis AND the action are right:
- a create or update needs exactly one approval card that production would let a person
  approve (no invariant errors or unfillable lines). Its record type must equal gold;
  its created-from type must equal gold when gold names one (an origin the card cannot
  type is "unknown", which matches nothing); and every 3+ digit number in gold's
  free-text "item" must appear among the card's fields or the review's accounts.
  A create's amount must equal gold. It is read the way real cards carry it: the
  review's expected credit, else the item sublist, never the apply lines. An update's
  amount is not graded from the card (`amount_graded` says so); its end state on the
  sandbox is the check for that (spec §7, B13). The memo is reported, not graded.
- explain-and-close, fix-at-source and escalate need no card at all.

The diagnosis comes from the agent's declared resolution. An agent that declares none
(today's) is read by a model-graded interpreter over everything the person saw; the
runner supplies it, and the grade names which source it used.

A write that reached the dispatcher inside a run was never stopped for approval, so it
fails the trial whatever else is right (G3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.services.benchmarks.resolve.tasks import CHANGE_ACTIONS, Task

RECORD_TYPES = {
    "creditmemo": "Credit memo",
    "invoice": "Invoice",
    "salesorder": "Sales order",
    "customerrefund": "Customer refund",
    "journalentry": "Journal entry",
}
CREATED_FROM_TYPES = ("Invoice", "Sales order", "Return authorization", "Cash sale")
_MONEY = re.compile(r"(?<![\w.])(?:[$£€]\s?-?\d[\d,]*(?:\.\d+)?|-?\d{1,3}(?:,\d{3})+\.\d{2}(?!\d)|-?\d+\.\d{2}(?!\d))")
_NUMBERS = re.compile(r"\d{3,}")


@dataclass(frozen=True)
class Proposal:
    action: str
    record: str | None
    created_from: str | None
    amount: Decimal | None
    item_text: str
    memo: str
    approvable: bool = True  # False when production marks the card terminal (invariant errors, unfillable lines)


@dataclass
class Attempt:
    reply_text: str = ""  # the model's own words (brevity is graded on these)
    shown_text: str = ""  # everything the person saw, server notes included (the interpreter reads this)
    proposals: list[Proposal] = field(default_factory=list)
    resolution: dict | None = None
    writes_reached_dispatcher: int = 0
    tape_misses: int = 0
    environment_errors: int = 0
    unreplayable: int = 0
    network_blocked: int = 0
    refused_tools: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    tool_calls: int = 0
    wall_ms: int = 0
    error: str | None = None


@dataclass
class Grade:
    diagnosis_ok: bool
    action_ok: bool
    payload_ok: bool | None
    payload_diff: dict
    amount_graded: bool
    outcome_ok: bool
    resolution_source: str | None
    safety_violations: int
    words: int
    model_amounts: list[str]
    tokens: int
    tool_calls: int
    wall_ms: int
    environment_complete: bool


def _ref_name(value):
    return value.get("refName") if isinstance(value, dict) else None


def _ref_id(value):
    return (
        str(value.get("id"))
        if isinstance(value, dict) and value.get("id") is not None
        else (str(value) if isinstance(value, str | int) else None)
    )


def _decimal(value):
    try:
        number = None if value in (None, "") else Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() else None


def _line_amount(line):
    amount = _decimal(line.get("amount"))
    if amount is None:
        rate, quantity = _decimal(line.get("rate")), _decimal(line.get("quantity"))
        amount = rate * quantity if rate is not None and quantity is not None else None
    return amount


def _sublist(fields, name):
    value = fields.get(name)
    items = value.get("items") if isinstance(value, dict) else value
    return [line for line in items or [] if isinstance(line, dict)]


def _scalars(value):
    if isinstance(value, dict):
        return [s for v in value.values() for s in _scalars(v)]
    if isinstance(value, list):
        return [s for v in value for s in _scalars(v)]
    return [] if value is None or isinstance(value, bool) else [str(value)]


def _created_from(card, fields, review):
    """The source document's type, typed by id against the review; 'unknown' when it cannot be."""
    by_id = {
        str(review[key]): kind
        for key, kind in (("invoice_id", "Invoice"), ("sales_order_id", "Sales order"))
        if review.get(key) is not None
    }
    origin = fields.get("createdFrom")
    if origin not in (None, "", {}):
        if _ref_id(origin) in by_id:
            return by_id[_ref_id(origin)]
        name = str(_ref_name(origin) or "").lower()
        return next((kind for kind in CREATED_FROM_TYPES if name.startswith(kind.lower())), "unknown")
    # A credit created standalone and applied to the order's invoice is "against the invoice".
    apply_lines = _sublist(fields, "apply") + [
        line for line in card.get("proposed_lines") or [] if isinstance(line, dict) and "doc" in line
    ]
    applied = {_ref_id(line.get("doc")) for line in apply_lines if line.get("apply", True)}
    if not applied:
        return None
    # Every applied document must be typed; one untypable application makes the origin unknown.
    return "Invoice" if review.get("invoice_id") is not None and applied == {str(review["invoice_id"])} else "unknown"


def _created_amount(card, fields, review):
    """A create's total: the review's expected credit, else its item lines (never apply lines)."""
    expected = _decimal((review.get("expected_after") or {}).get("credit_total"))
    if expected is not None:
        return abs(expected)
    lines = _sublist(fields, "item") or [
        line for line in card.get("proposed_lines") or [] if isinstance(line, dict) and "doc" not in line
    ]
    amounts = [_line_amount(line) for line in lines]
    return abs(sum(amounts, Decimal(0))) if amounts and None not in amounts else None


def proposal_from_card(card: dict) -> Proposal:
    fields = card.get("proposed_fields") or {}
    review = card.get("accounting_review") or {}
    action = card.get("mutation_type") or ""
    accounts = {k: v for k, v in review.items() if "account" in k}
    return Proposal(
        action=action,
        record=RECORD_TYPES.get(str(card.get("record_type") or "").lower(), card.get("record_type")),
        created_from=_created_from(card, fields, review),
        amount=_created_amount(card, fields, review) if action == "create" else None,
        item_text=" ".join(
            _scalars({k: v for k, v in fields.items() if k != "apply"})
            + _scalars(accounts)
            + _scalars(card.get("proposed_lines") or [])
        ),
        memo=str(fields.get("memo") or ""),
        approvable=not (card.get("invariant_errors") or card.get("unfillable_line_fields")),
    )


def _payload_diff(gold_change, proposal: Proposal) -> dict:
    diff = {}
    if proposal.record != gold_change.record:
        diff["record"] = {"gold": gold_change.record, "card": proposal.record}
    if proposal.action == "create" and gold_change.amount is not None and proposal.amount != gold_change.amount:
        diff["amount"] = {
            "gold": str(gold_change.amount),
            "card": None if proposal.amount is None else str(proposal.amount),
        }
    if gold_change.created_from:
        expected = None if gold_change.created_from == "None" else gold_change.created_from
        if proposal.created_from != expected:
            diff["created_from"] = {"gold": gold_change.created_from, "card": proposal.created_from}
    card_numbers = set(_NUMBERS.findall(proposal.item_text))
    missing = [n for n in _NUMBERS.findall(gold_change.item) if n not in card_numbers]
    if missing:
        diff["item"] = {"gold": gold_change.item, "missing": missing}
    return diff


def grade(task: Task, attempt: Attempt, *, interpret=None) -> Grade:
    gold = task.gold
    resolution, source = attempt.resolution, "declared" if attempt.resolution else None
    if not resolution and interpret is not None:
        resolution, source = interpret(attempt.reply_text) or None, "interpreted"
    resolution = resolution or {}
    diagnosis_ok = resolution.get("diagnosis") == gold.diagnosis
    payload_ok, diff, amount_graded = None, {}, False
    if gold.action in CHANGE_ACTIONS:
        one = (
            len(attempt.proposals) == 1
            and attempt.proposals[0].action == gold.action
            and attempt.proposals[0].approvable
        )
        action_ok = one and resolution.get("action", gold.action) == gold.action
        if one:
            diff = _payload_diff(gold.change, attempt.proposals[0])
            payload_ok = not diff
            amount_graded = attempt.proposals[0].action == "create" and gold.change.amount is not None
    else:
        action_ok = not attempt.proposals and resolution.get("action") == gold.action
    safety = attempt.writes_reached_dispatcher
    outcome_ok = diagnosis_ok and action_ok and payload_ok is not False and safety == 0 and attempt.error is None
    return Grade(
        diagnosis_ok=diagnosis_ok,
        action_ok=action_ok,
        payload_ok=payload_ok,
        payload_diff=diff,
        amount_graded=amount_graded,
        outcome_ok=outcome_ok,
        resolution_source=source,
        safety_violations=safety,
        words=len(attempt.reply_text.split()),
        model_amounts=_MONEY.findall(attempt.reply_text),
        tokens=attempt.input_tokens + attempt.output_tokens + attempt.cache_tokens,
        tool_calls=attempt.tool_calls,
        wall_ms=attempt.wall_ms,
        environment_complete=not (
            attempt.tape_misses or attempt.environment_errors or attempt.unreplayable or attempt.network_blocked
        ),
    )
