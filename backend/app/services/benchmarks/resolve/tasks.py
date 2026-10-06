"""Benchmark tasks and the gold labels a person wrote for them.

Gold comes from the labelling page's `labels` collection, exported as one JSON file per
order (`<dir>/<ref>.json`, or `<dir>/labels/<ref>.json` as an artifact export writes it).
The vocabularies below are the page's options; a label outside them is a mistake to fix
at the source, so loading fails loudly instead of guessing.

The held-out quarter is chosen by hash (`held_out_refs`), the same rule the labelling
sheet used, so the split never depends on file order. Held-out gold loads only when a
caller asks for it by name.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

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
CHANGE_ACTIONS = frozenset({"create", "update"})
RECORDS = frozenset({"Credit memo", "Invoice", "Sales order", "Customer refund", "Journal entry", "Other"})
SPLIT_SALT = "resolve-bench:"
HELD_OUT_FRACTION = 0.25
PROMPT = "Resolve the reconciliation case {case_id} for order {ref}."

Split = Literal["held_in", "held_out", "all"]


@dataclass(frozen=True)
class Change:
    record: str
    created_from: str | None
    amount: Decimal | None
    item: str
    memo: str


@dataclass(frozen=True)
class Gold:
    diagnosis: str
    action: str
    change: Change | None
    evidence: str


@dataclass(frozen=True)
class Task:
    ref: str
    case_id: str
    prompt: str
    gold: Gold | None


def held_out_refs(refs) -> frozenset[str]:
    ordered = sorted(set(refs), key=lambda r: hashlib.sha256(f"{SPLIT_SALT}{r}".encode()).hexdigest())
    return frozenset(ordered[: round(len(ordered) * HELD_OUT_FRACTION)])  # the labelling sheet's rule


def _amount(ref, value):
    if value in (None, ""):
        return None
    try:
        return abs(Decimal(str(value).replace("$", "").replace(",", "").strip()))
    except InvalidOperation:
        raise ValueError(f"{ref}: change amount {value!r} is not a number") from None


def _gold(ref, label) -> Gold | None:
    if label.get("order_reference") not in (None, ref):
        raise ValueError(f"{ref}: label names order {label.get('order_reference')!r}")
    if label.get("confidence") != "sure":
        return None  # "Not sure: needs a second look" is not gold yet
    diagnosis, action = label.get("diagnosis"), label.get("action")
    if diagnosis not in DIAGNOSES:
        raise ValueError(f"{ref}: unknown diagnosis {diagnosis!r}")
    if action not in ACTIONS:
        raise ValueError(f"{ref}: unknown action {action!r}")
    change = None
    if action in CHANGE_ACTIONS:
        raw = label.get("change") or {}
        if raw.get("record") not in RECORDS:
            raise ValueError(f"{ref}: a {action} needs the record it changes")
        change = Change(
            record=raw["record"],
            created_from=raw.get("created_from") or None,
            amount=_amount(ref, raw.get("amount")),
            item=(raw.get("item") or "").strip(),
            memo=(raw.get("memo") or "").strip(),
        )
    return Gold(diagnosis=diagnosis, action=action, change=change, evidence=(label.get("evidence") or "").strip())


def _labels(labels_dir: Path | None) -> dict[str, dict]:
    if labels_dir is None:
        return {}
    root = Path(labels_dir)
    folder = root / "labels" if (root / "labels").is_dir() else root
    return {path.stem: json.loads(path.read_text()) for path in sorted(folder.glob("*.json"))}


def load_tasks(tasks_path, labels_dir=None, *, split: Split = "held_in", require_gold: bool = True) -> list[Task]:
    rows = json.loads(Path(tasks_path).read_text())
    refs = [row["ref"] for row in rows]
    held = held_out_refs(refs)
    labels = _labels(labels_dir)
    out = []
    for row in rows:
        ref = row["ref"]
        if (split == "held_in" and ref in held) or (split == "held_out" and ref not in held):
            continue
        gold = _gold(ref, labels[ref]) if ref in labels else None
        if require_gold and gold is None:
            continue
        out.append(
            Task(ref=ref, case_id=row["case_id"], prompt=PROMPT.format(ref=ref, case_id=row["case_id"]), gold=gold)
        )
    return out
