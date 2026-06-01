"""`sample_dataset` — the deterministic demo data source for the rich pipe.

A real, local Hermes tool that returns a fixed, non-empty finance table with no
network and no randomness. It is the data source for rich-pipe slice 1: the
agent calls it, the orchestration seed intercepts its `{columns, rows}` result,
and converts it into a webapp-shaped `data_table` event that the reused
`data-frame-table` card renders. Chosen over the wired `obsidian-memory.read_graph`
because a fresh vault is empty (an empty table proves nothing).
"""

from __future__ import annotations

import json
from typing import Any

# Toolset name. With `enabled_toolsets=None` (the sidecar default) Hermes exposes
# every registered toolset whose check_fn passes, so registering here is what
# makes the live agent able to call the tool.
_TOOLSET = "suite_studio_demo"

SAMPLE_DATASET_SCHEMA: dict[str, Any] = {
    "name": "sample_dataset",
    "description": (
        "Return a small, fixed, deterministic finance sample table (account "
        "balances) for demos and onboarding. Takes no arguments and makes no "
        "network calls. Use this when the user asks to see sample data, a demo "
        "table, or example account balances."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def sample_dataset() -> dict[str, Any]:
    """Return a fresh, deterministic finance sample table.

    Shape mirrors what the orchestration seed intercepts:
    ``{"columns": [<str>, ...], "rows": [[...], ...]}``. A brand-new structure is
    built on every call so callers may transform it without cross-call leakage.
    """
    return {
        "columns": ["Account", "Balance (USD)"],
        "rows": [
            ["Cash & Equivalents", 1284500.00],
            ["Accounts Receivable", 842300.50],
            ["Inventory", 415900.00],
            ["Accounts Payable", -298750.25],
            ["Retained Earnings", 2243950.25],
        ],
    }


def sample_dataset_handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Hermes tool handler: return the dataset as a JSON string.

    Hermes invokes registered handlers as ``handler(args, **kwargs)`` and treats
    the return value as the tool result. ``tool_complete_callback`` then receives
    that string as ``tool_result``; the orchestration seed json.loads it back
    into ``{columns, rows}``. The handler ignores its arguments — the tool takes
    none.
    """
    return json.dumps(sample_dataset(), ensure_ascii=False)


def register(registry: Any) -> None:
    """Register `sample_dataset` with a Hermes tool registry instance."""
    registry.register(
        name="sample_dataset",
        toolset=_TOOLSET,
        schema=SAMPLE_DATASET_SCHEMA,
        handler=sample_dataset_handler,
        check_fn=lambda: True,  # always available — no env/network dependency
        description=SAMPLE_DATASET_SCHEMA["description"],
        emoji="\U0001F4CA",  # 📊
    )


# Module-import registration side effect — mirrors Hermes' own tool files
# (e.g. tools/todo_tool.py register at import time). Guarded against a missing
# Hermes so importing the pure `sample_dataset()` data source never hard-fails
# in a registry-free context; the registration itself is proven by
# test_sample_dataset.py::test_registered_with_hermes_registry_so_the_agent_can_call_it.
try:  # pragma: no cover - exercised indirectly by the registration test
    from tools.registry import registry as _registry
except ImportError:  # Hermes registry unavailable — pure-data use still works.
    pass
else:
    register(_registry)
