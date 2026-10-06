"""Resolver skills: a proven fix for one cause, written down (spec 2026-10-01 §5.5, block B7).

A skill is a file in this package (`<name>.md`): YAML front matter, then prose for the
agent. The front matter says:
- `checks`: when it applies. These are names from `CHECKS`, a closed set of code
  functions over the case file (`case_file`, B5) and the live chain (`resolver_reads`,
  B6), so a skill can only use checks that exist and are tested.
- `change`: the change it proposes. A template: the record and item are fixed, while the
  amount, source document and memo come from the case, never from the skill.
- `verify`: how the result is checked after approval.
- `evidence`: the verified bookings it was written from.

`status` is `proposed` until Aiden approves (`approved_by`, `approved_at`). Only approved
skills are ever applied; a proposed skill whose checks pass is reported as awaiting
approval. A malformed file fails at load. `skill_find` never writes: it returns the
resolved change for an approval card, or the checks that failed.
"""

from __future__ import annotations

import re
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
MEMO_FIELDS = frozenset({"order", "adjustment_label"})
_ITEM = re.compile(r"^[1-9][0-9]{0,11}$")


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
        self.order = (case_file.get("case") or {}).get("order")
        self.adjustment_labels = list((case_file.get("facts") or {}).get("adjustments_equal_to_difference") or [])
        self.chain_complete = chain.get("complete") is True
        self.top = chain.get("top")
        docs = [d for d in chain.get("documents") or [] if isinstance(d, dict)]
        self.top_doc = next((d for d in docs if d.get("id") == self.top), None)
        self.invoices = [d for d in docs if d.get("type") == "invoice" and d.get("created_from") == self.top]
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
    if skill["action"] in {"create", "update"}:
        _require(isinstance(change, dict), name, "a create or update needs a change")
        _require(isinstance(change.get("record_type"), str), name, "change.record_type is required")
        _require(change.get("created_from") in CREATED_FROM | {None}, name, "unknown change.created_from")
        lines = change.get("lines")
        _require(isinstance(lines, list) and lines, name, "change.lines are required")
        for line in lines:
            _require(isinstance(line, dict) and _ITEM.match(str(line.get("item", ""))), name, "a line needs an item id")
            # Amounts come from the case, never from the skill.
            _require(line.get("amount") == "difference", name, "a line amount must be 'difference'")
        fields = set(re.findall(r"{(\w+)}", str(change.get("memo") or "")))
        _require(fields <= MEMO_FIELDS, name, f"memo may only use {sorted(MEMO_FIELDS)}")
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


def _resolve(change: dict, c: _Case) -> dict:
    out = {"record_type": change["record_type"]}
    if change.get("created_from") == "invoice":
        invoice = c.invoices[0]
        out["created_from"] = {"type": "invoice", "id": invoice.get("id"), "number": invoice.get("number")}
    out["lines"] = [{"item": str(line["item"]), "amount": f"{abs(c.difference):.2f}"} for line in change["lines"]]
    if change.get("memo"):
        out["memo"] = (
            change["memo"].format(order=c.order or "", adjustment_label=(c.adjustment_labels or [""])[0]).strip()
        )
    return out


def skill_find(case_file: dict, chain: dict, *, library: dict | None = None) -> dict:
    """The approved skill whose checks all pass on this case, with its change resolved, or why none applies."""
    library = load_library() if library is None else library
    c = _Case(case_file, chain)
    matches, near, awaiting = [], [], []
    for name, skill in sorted(library.items()):
        if skill["status"] == "retired":
            continue
        results = []
        for check in skill["checks"]:
            passed, detail = CHECKS[check](c)
            results.append({"check": check, "passed": bool(passed), "detail": detail})
        failed = [r for r in results if not r["passed"]]
        if failed:
            near.append({"skill": name, "failed": failed})
        elif skill["status"] == "proposed":
            awaiting.append(name)
        else:
            matches.append((name, skill, results))
    if len(matches) != 1:
        # None, or more than one: never guess between skills.
        return {"match": None, "near": near, "awaiting_approval": awaiting, "ambiguous": [m[0] for m in matches]}
    name, skill, results = matches[0]
    return {
        "match": {
            "skill": name,
            "version": skill["version"],
            "diagnosis": skill["diagnosis"],
            "action": skill["action"],
            "change": _resolve(skill["change"], c) if skill.get("change") else None,
            "checks": results,
            "verify": skill["verify"],
        },
        "near": near,
        "awaiting_approval": awaiting,
        "ambiguous": [],
    }
