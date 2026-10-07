"""Resolver skills: a proven fix for one cause, written down (spec 2026-10-01 §5.5, block B7).

A skill is a file in this package (`<name>.md`): YAML front matter, then prose for the
agent. The front matter says:
- `checks`: when it applies. These are names from `CHECKS`, a closed set of code
  functions over the case file (`case_file`, B5) and the live chain (`resolver_reads`,
  B6), so a skill can only use checks that exist and are tested.
- `change` (a `create` only): the change it proposes. A template with one line: the
  record and item are fixed, while the amount, source document and memo come from the
  case, never from the skill. Update skills are refused until there is a validator for
  them, and other actions carry no change.
- `verify`: how the result is checked after approval.
- `evidence`: the verified bookings it was written from.

`status` is `proposed` until Aiden approves (`approved_by`, `approved_at`). Only approved
skills are ever applied; a proposed skill whose checks pass is reported as awaiting
approval. A malformed file fails at load. Whatever checks a skill lists, two always
hold: the chain's top document must be this case's own sales order (and an invoice
counts only when created from it), and the change must resolve from the case's own
evidence (a non-zero, whole-cent difference, used exactly; one invoice with an id whose
total less the credit equals the Solidus total; and each field the memo uses:
an order number, and one text label when the memo names the adjustment). `skill_find`
never writes: it returns the resolved change for an approval card, or the checks that
failed.
"""

from __future__ import annotations

import re
import string
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

LIBRARY_DIR = Path(__file__).parent
STATUSES = frozenset({"proposed", "approved", "retired"})
# The labelling page's vocabularies, so a skill's outcome grades against gold directly.
DIAGNOSES = frozenset(
    {
        "credited",
        "explained_other",
        "needs_credit_memo",
        "business_pricing",
        "billing_gap",
        "fix_source",
        "netsuite_wrong_other",
        "data_issue",
        "other",
    }
)
ACTIONS = frozenset({"explain_close", "create", "update", "fix_source", "escalate"})
CREATED_FROM = frozenset({"invoice"})
RECORD_TYPES = frozenset({"creditMemo"})  # widened only with a resolver and validator for the new type
MEMO_FIELDS = frozenset({"order", "adjustment_label"})
_ITEM = re.compile(r"[1-9][0-9]{0,11}")


def _internal_id(value) -> bool:
    return isinstance(value, str) and _ITEM.fullmatch(value) is not None


def _amount(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


class _Case:
    """What the checks may look at, read defensively: missing evidence fails a check, never raises."""

    def __init__(self, case_file: dict, chain: dict):
        case_file, chain = case_file or {}, chain or {}
        metrics = ((case_file.get("comparison") or {}).get("metrics") or {}).get("order_total") or {}
        self.difference = _amount(metrics.get("difference"))
        self.solidus_total = _amount(metrics.get("solidus"))
        self.order = (case_file.get("case") or {}).get("order")
        self.adjustment_labels = list((case_file.get("facts") or {}).get("adjustments_equal_to_difference") or [])
        self.chain_complete = chain.get("complete") is True
        self.top = chain.get("top") if _internal_id(chain.get("top")) else None
        docs = [d for d in chain.get("documents") or [] if isinstance(d, dict)]
        self.top_doc = next((d for d in docs if self.top and d.get("id") == self.top), None)
        # The chain must be this case's own sales order; nothing else may supply its documents.
        self.same_order = (
            isinstance(self.order, str)
            and bool(self.order)
            and self.top_doc is not None
            and self.top_doc.get("type") == "sales order"
            and self.top_doc.get("number") == self.order
        )
        self.invoices = [
            d for d in docs if self.top and d.get("type") == "invoice" and d.get("created_from") == self.top
        ]
        invoice_ids = {d.get("id") for d in self.invoices}
        self.credits_from_invoice = [
            d for d in docs if d.get("type") == "credit memo" and d.get("created_from") in invoice_ids
        ]


def _solidus_below_netsuite(c: _Case):
    if c.difference is None:
        return False, "no order-total difference in the case file"
    return c.difference < 0, f"difference {c.difference}"


def _adjustment_equals_difference(c: _Case):
    if len(c.adjustment_labels) != 1:
        return False, f"{len(c.adjustment_labels)} order adjustments equal the difference (exactly one is required)"
    return True, f"'{c.adjustment_labels[0]}' equals the difference"


def _chain_complete(c: _Case):
    return (
        c.chain_complete,
        "the live chain was read completely" if c.chain_complete else "the live chain is incomplete",
    )


def _one_invoice_from_the_order(c: _Case):
    if not c.top_doc or c.top_doc.get("type") != "sales order":
        return False, "the chain's top document is not a sales order"
    return len(c.invoices) == 1, f"{len(c.invoices)} invoices created from the order (exactly one is required)"


def _no_credit_from_the_invoice(c: _Case):
    if c.credits_from_invoice:
        numbers = ", ".join(str(d.get("number")) for d in c.credits_from_invoice)
        return False, f"credit memos already created from the invoice: {numbers}"
    return True, "no credit memo created from the invoice"


CHECKS = {
    "solidus_below_netsuite": _solidus_below_netsuite,
    "adjustment_equals_difference": _adjustment_equals_difference,
    "chain_complete": _chain_complete,
    "one_invoice_from_the_order": _one_invoice_from_the_order,
    "no_credit_from_the_invoice": _no_credit_from_the_invoice,
}


def _require(condition, name, message):
    if not condition:
        raise ValueError(f"skill {name}: {message}")


def _validate(skill: dict, source: Path) -> dict:
    name = skill.get("name")
    _require(isinstance(name, str) and name, source.name, "name is required")
    _require(type(skill.get("version")) is int and skill["version"] >= 1, name, "version must be a whole number")
    _require(skill.get("status") in STATUSES, name, f"status must be one of {sorted(STATUSES)}")
    if skill["status"] == "approved":
        _require(
            skill.get("approved_by") and skill.get("approved_at"),
            name,
            "an approved skill needs approved_by and approved_at",
        )
    _require(skill.get("diagnosis") in DIAGNOSES, name, "unknown diagnosis")
    _require(skill.get("action") in ACTIONS, name, "unknown action")
    checks = skill.get("checks")
    _require(isinstance(checks, list) and checks, name, "checks are required")
    unknown = [c for c in checks if c not in CHECKS]
    _require(not unknown, name, f"unknown check {unknown}")
    for key in ("cause",):
        _require(isinstance(skill.get(key), str) and skill[key], name, f"{key} is required")
    for key in ("verify", "evidence"):
        _require(isinstance(skill.get(key), list) and skill[key], name, f"{key} is required")
    change = skill.get("change")
    _require(skill["action"] != "update", name, "update skills are not supported yet (no validator for an update)")
    if skill["action"] != "create":
        _require(change is None, name, f"a {skill['action']} skill carries no change")
        return skill
    _require(isinstance(change, dict), name, "a create needs a change")
    records = sorted(RECORD_TYPES)
    _require(change.get("record_type") in RECORD_TYPES, name, f"change.record_type must be one of {records}")
    origins = sorted(CREATED_FROM)
    _require(change.get("created_from") in CREATED_FROM, name, f"change.created_from must be one of {origins}")
    lines = change.get("lines")
    # The whole difference goes to one line; two lines would each carry it.
    _require(isinstance(lines, list) and len(lines) == 1, name, "a change has exactly one line")
    line = lines[0]
    _require(isinstance(line, dict) and _internal_id(line.get("item")), name, "a line needs an item id")
    # Amounts come from the case, never from the skill.
    _require(line.get("amount") == "difference", name, "a line amount must be 'difference'")
    memo = change.get("memo")
    _require(memo is None or isinstance(memo, str), name, "memo must be text")
    try:
        parts = list(string.Formatter().parse(memo or ""))
    except ValueError:
        parts = None
    _require(parts is not None, name, "memo is not a valid template")
    for _literal, field, spec, conversion in parts:
        if field is not None:
            _require(
                field in MEMO_FIELDS and not spec and conversion is None,
                name,
                f"memo may only use plain {{order}} and {{adjustment_label}}, not {{{field}}}",
            )
    return skill


def load_library(directory: Path | None = None) -> dict[str, dict]:
    library = {}
    for path in sorted(Path(directory or LIBRARY_DIR).glob("*.md")):
        text = path.read_text()
        match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        _require(match, path.name, "front matter is required")
        skill = yaml.safe_load(match.group(1)) or {}
        _require(isinstance(skill, dict), path.name, "front matter must be a mapping")
        skill = _validate(skill, path)
        _require(skill["name"] not in library, skill["name"], "duplicate name")
        library[skill["name"]] = {**skill, "prose": match.group(2).strip()}
    return library


def _resolve(change: dict, c: _Case) -> tuple[dict | None, str | None]:
    """The change for this case, or why it cannot be resolved, whatever checks the skill chose."""
    if c.difference is None or c.difference == 0:
        return None, "the case has no non-zero order-total difference"
    amount = abs(c.difference)
    # Never rounded: an amount that is not a whole number of cents is declined, not changed.
    if amount.quantize(Decimal("0.01")) != amount:
        return None, f"the difference {c.difference} is not a whole-cent amount"
    out = {"record_type": change["record_type"]}
    if change.get("created_from") == "invoice":
        invoice = c.invoices[0] if len(c.invoices) == 1 else {}
        if not _internal_id(invoice.get("id")):
            return None, "exactly one invoice with an id is needed"
        # A sales-order difference does not prove the invoice is wrong: credit only when the
        # invoice less this credit equals what Solidus charged.
        total = _amount(invoice.get("total"))
        if total is None or c.solidus_total is None or total - amount != c.solidus_total:
            return None, (
                f"the invoice ({invoice.get('total')}) less the credit ({amount}) does not equal "
                f"the Solidus total ({c.solidus_total})"
            )
        out["created_from"] = {"type": "invoice", "id": invoice["id"], "number": invoice.get("number")}
    exact = f"{amount.quantize(Decimal('0.01'))}"  # equal to amount: checked above
    out["lines"] = [{"item": str(line["item"]), "amount": exact} for line in change["lines"]]
    memo = change.get("memo")
    if memo:
        fields = {field for _l, field, _s, _c in string.Formatter().parse(memo) if field}
        label = c.adjustment_labels[0] if len(c.adjustment_labels) == 1 else None
        values = {"order": c.order, "adjustment_label": label}
        missing = [f for f in sorted(fields) if not (isinstance(values[f], str) and values[f].strip())]
        if missing:
            return None, f"the memo needs {', '.join(missing)}"
        out["memo"] = memo.format(**{f: values[f].strip() for f in fields}).strip()
    return out, None


def skill_find(case_file: dict, chain: dict, *, library: dict | None = None) -> dict:
    """The approved skill whose checks all pass on this case, with its change resolved, or why none applies."""
    library = load_library() if library is None else library
    c = _Case(case_file, chain)
    matches, near, awaiting = [], [], []
    for name, skill in sorted(library.items()):
        if skill["status"] == "retired":
            continue
        results = [
            {
                "check": "chain_belongs_to_case",
                "passed": c.same_order,
                "detail": "the chain's top document is this case's sales order"
                if c.same_order
                else "the chain is not this case's sales order",
            }
        ]
        for check in skill["checks"]:
            passed, detail = CHECKS[check](c)
            results.append({"check": check, "passed": bool(passed), "detail": detail})
        change = None
        if skill.get("change") and all(r["passed"] for r in results):
            change, problem = _resolve(skill["change"], c)
            if problem:
                results.append({"check": "change_resolves", "passed": False, "detail": problem})
        failed = [r for r in results if not r["passed"]]
        if failed:
            near.append({"skill": name, "failed": failed})
        elif skill["status"] == "proposed":
            awaiting.append(name)
        else:
            matches.append((name, skill, results, change))
    if len(matches) != 1:
        # None, or more than one: never guess between skills.
        return {"match": None, "near": near, "awaiting_approval": awaiting, "ambiguous": [m[0] for m in matches]}
    name, skill, results, change = matches[0]
    return {
        "match": {
            "skill": name,
            "version": skill["version"],
            "diagnosis": skill["diagnosis"],
            "action": skill["action"],
            "change": change,
            "checks": results,
            "verify": skill["verify"],
        },
        "near": near,
        "awaiting_approval": awaiting,
        "ambiguous": [],
    }
