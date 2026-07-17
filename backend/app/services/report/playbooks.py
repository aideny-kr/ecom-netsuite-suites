"""Playbook catalog — curated deterministic report recipes (no LLM in the loop).

Each playbook maps 1:1 to a netsuite_financial_report REPORT_TEMPLATE, so every
number is a statement-grade GL aggregate. build_playbook_recipe emits exactly
the recipe_json schema the refresh engine validates, which is what buys playbook
reports auto-refresh, versioning, and download with zero extra machinery.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

_PERIOD_RE = re.compile(r"^[A-Z][a-z]{2} \d{4}$")  # NetSuite period name: "Jun 2026"

PLAYBOOKS: dict[str, dict] = {
    "income_statement": {
        "name": "Income Statement",
        "description": "Statement-grade P&L for one accounting period, straight from the GL.",
        "params": [{"key": "period", "label": "Accounting period", "example": "Jun 2026"}],
        "table_label": "P&L by account",
    },
    "balance_sheet": {
        "name": "Balance Sheet",
        "description": "Balance Sheet as of the end of an accounting period (inception-to-date).",
        "params": [{"key": "period", "label": "As-of period", "example": "Jun 2026"}],
        "table_label": "Balances by account",
    },
    "trial_balance": {
        "name": "Trial Balance",
        "description": "All GL accounts with debit/credit totals for one accounting period.",
        "params": [{"key": "period", "label": "Accounting period", "example": "Jun 2026"}],
        "table_label": "Trial balance",
    },
}


def build_playbook_recipe(playbook_key: str, params: dict[str, str]) -> tuple[str, dict]:
    meta = PLAYBOOKS.get(playbook_key)
    if meta is None:
        raise ValueError(f"Unknown playbook: '{playbook_key}'")
    period = (params or {}).get("period", "").strip()
    if not _PERIOD_RE.match(period):
        raise ValueError("period must be a NetSuite period name like 'Jun 2026'")
    title = f"{meta['name']} — {period}"
    recipe = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sections": [
            {"type": "heading", "level": 1, "text": title},
            {"type": "table", "result_id": "r1", "label": meta["table_label"]},
            {
                "type": "narrative",
                "markdown": (
                    f"{{{{result:r1.row_count}}}} GL lines. Generated deterministically by the "
                    f"{meta['name']} playbook — every figure is a GL aggregate; no model wrote a number."
                ),
            },
        ],
        "sources": {
            "r1": {
                "tool": "netsuite_financial_report",
                "params": {"report_type": playbook_key, "period": period},
                "connection_id": None,
            }
        },
    }
    return title, recipe
