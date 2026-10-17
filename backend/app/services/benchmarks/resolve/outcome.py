"""The outcome grader: a fix is right when the order then equals Solidus.

Decided by Aiden on 2026-10-10 over waiting for hand labels (0 of 72): it covers every open
case, and it grades our agent and native Claude + NetSuite MCP the same way.

- Each case is captured once as a ``Snapshot``: the order's NetSuite documents (the invoice,
  every credit and their GL), the finalized Solidus order, and the booking profile and items,
  exactly as ``credit_creation.gather`` reads them for the server's own credit check. Both
  agents are graded against the same snapshot, whatever they each chose to read.
- The snapshot says what kind of answer is right. Balanced: "NetSuite is already right", so
  no write. NetSuite above Solidus: one credit memo. A credit passes only through
  ``credit_creation.assess``, the server's own acceptance (total AND tax to the cent, credit
  items and income accounts, tax only through configured tax items, room on the invoice),
  plus the booking playbook (non-taxable lines, applied to the order's invoice only, the
  order's customer, a memo that names the order).
- Anything the engine cannot grade yet (NetSuite below Solidus, a tax-only difference, an
  update, a shape ``gather`` refuses) is ungraded (``ok is None``) with its reason, so the
  report counts it instead of silently dropping it.

Snapshots hold real customer data: they live outside the repository, like benchmark results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from app.services.transaction_ops import credit_creation
from app.services.transaction_ops.credit_line_reallocation import RefusalError

SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class Write:
    """A NetSuite write an agent proposed: our approval card, or native Claude's MCP call."""

    action: str  # "create" | "update"
    record_type: str  # lower case, e.g. "creditmemo"
    fields: dict


@dataclass(frozen=True)
class OutcomeGrade:
    ok: bool | None  # None: the engine cannot grade this shape yet
    reason: str
    detail: dict = field(default_factory=dict)


def _encode(value):
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__decimal__"}:
            return Decimal(value["__decimal__"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


@dataclass(frozen=True)
class Snapshot:
    ref: str
    case_id: str
    found: dict | None = None  # credit_creation.gather's facts
    refusal: str | None = None  # gather refused this order's shape

    @classmethod
    def taken(cls, ref, case_id, found):
        return cls(ref=ref, case_id=str(case_id), found=found)

    @classmethod
    def refused(cls, ref, case_id, code):
        return cls(ref=ref, case_id=str(case_id), refusal=code)

    def to_json(self) -> dict:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "ref": self.ref,
            "case_id": self.case_id,
            "found": _encode(self.found) if self.found is not None else None,
            "refusal": self.refusal,
        }

    @classmethod
    def from_json(cls, body: dict) -> Snapshot:
        if body.get("snapshot_version") != SNAPSHOT_VERSION:
            raise ValueError("unknown snapshot version; capture it again")
        found = _decode(body["found"]) if body.get("found") is not None else None
        return cls(ref=body["ref"], case_id=body["case_id"], found=found, refusal=body.get("refusal"))


async def capture(db, tenant_id, case_id, ref, item_ids) -> Snapshot:
    """Read the order once, as the server's credit check reads it (read-only)."""
    try:
        found, _ = await credit_creation.gather(db, tenant_id, case_id, list(item_ids))
    except RefusalError as exc:
        return Snapshot.refused(ref, case_id, exc.code)
    return Snapshot.taken(ref, case_id, found)


def _record_type(value) -> str:
    return str(value or "").replace("_", "").replace(" ", "").lower()


def write_from_card(card: dict) -> Write | None:
    """Our agent's approval card as the write it would post."""
    if not isinstance(card, dict) or card.get("mutation_type") not in ("create", "update"):
        return None
    fields = card.get("proposed_fields")
    return Write(
        card["mutation_type"], _record_type(card.get("record_type")), fields if isinstance(fields, dict) else {}
    )


def write_from_call(tool_name: str, tool_input) -> Write | None:
    """Native Claude's NetSuite MCP write, as intercepted by the benchmark."""
    verb = str(tool_name).rsplit("__", 1)[-1]
    action = {"ns_createRecord": "create", "ns_updateRecord": "update"}.get(verb)
    if action is None or not isinstance(tool_input, dict):
        return None
    data = tool_input.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    return Write(action, _record_type(tool_input.get("recordType")), data if isinstance(data, dict) else {})


def _ref(value):
    return str(value.get("id")) if isinstance(value, dict) and value.get("id") is not None else None


def _items(fields):
    value = fields.get("item")
    items = value.get("items") if isinstance(value, dict) else value
    return [line for line in items or [] if isinstance(line, dict)]


def _line_amount(line):
    if line.get("amount") not in (None, ""):
        return Decimal(str(line["amount"]))
    if line.get("rate") not in (None, ""):
        return Decimal(str(line["rate"])) * Decimal(str(line.get("quantity") or 1))
    return None


def _classify(snapshot):
    """('balanced'|'over_posted', None) when the engine can grade the order, else (None, reason)."""
    if snapshot.refusal:
        return None, snapshot.refusal
    try:
        current = credit_creation.facts(require_difference=False, **snapshot.found)
    except RefusalError as exc:
        return None, exc.code
    (booked_total, booked_tax), (total, tax) = current["before"], current["required"]
    if (booked_total, booked_tax) == (total, tax):
        return "balanced", None
    if booked_total < total:
        return None, "netsuite_below_source"
    if booked_total == total:
        return None, "tax_only_difference"
    return "over_posted", None


def grade_outcome(snapshot: Snapshot, writes: list[Write]) -> OutcomeGrade:
    kind, reason = _classify(snapshot)
    if kind is None:
        return OutcomeGrade(None, reason)
    if any(w.action != "create" or w.record_type != "creditmemo" for w in writes):
        return (
            OutcomeGrade(None, "update_not_graded_yet")
            if kind == "over_posted"
            else OutcomeGrade(False, "write_on_a_balanced_order")
        )
    if kind == "balanced":
        return (
            OutcomeGrade(True, "netsuite_already_right")
            if not writes
            else OutcomeGrade(False, "write_on_a_balanced_order")
        )
    if not writes:
        return OutcomeGrade(False, "expected_a_credit")
    if len(writes) > 1:
        return OutcomeGrade(False, "one_credit_expected", {"credits": len(writes)})
    return _grade_credit(snapshot.found, writes[0].fields)


def _grade_credit(found, fields) -> OutcomeGrade:
    lines = []
    for line in _items(fields):
        if line.get("isTaxable") is True or line.get("taxCode") not in (None, "", {}):
            # NetSuite would compute tax the snapshot cannot; the playbook reverses tax through
            # configured tax items on non-taxable lines.
            return OutcomeGrade(False, "taxable_line_not_in_playbook")
        amount = _line_amount(line)
        if amount is None:
            return OutcomeGrade(False, "line_amount_missing")
        lines.append({"item_id": _ref(line.get("item")) or str(line.get("item") or ""), "amount": str(amount)})
    try:
        result = credit_creation.assess(lines=lines, memo=fields.get("memo") or "", **found)
    except RefusalError as exc:
        return OutcomeGrade(False, exc.code, dict(exc.detail or {}) if hasattr(exc, "detail") else {})
    expected = result["proposed_fields"]
    if _ref(fields.get("entity")) != _ref(expected["entity"]):
        return OutcomeGrade(False, "wrong_customer")
    for key in ("subsidiary", "currency"):
        if fields.get(key) not in (None, {}) and _ref(fields.get(key)) != _ref(expected[key]):
            return OutcomeGrade(False, f"wrong_{key}")
    applied = [
        a for a in (fields.get("apply") or {}).get("items") or [] if isinstance(a, dict) and a.get("apply", True)
    ]
    invoice = _ref(found["invoices"][0][0])
    if {_ref(a.get("doc")) for a in applied} != {invoice}:
        return OutcomeGrade(False, "applied_to_another_document")
    total = Decimal(result["expected_after"]["total"])
    if sum((Decimal(str(a.get("amount") or 0)) for a in applied), Decimal(0)) != total:
        return OutcomeGrade(False, "application_amount_differs")
    if not str(fields.get("memo") or "").strip().startswith(found["source"]["number"]):
        return OutcomeGrade(False, "memo_must_name_the_order")
    return OutcomeGrade(True, "order_equals_source", {"credit": str(total)})
