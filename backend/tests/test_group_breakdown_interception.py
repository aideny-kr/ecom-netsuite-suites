"""The breakdown reaches the screen as a card with every amount; the model reads no amounts."""

import json

from app.services.chat.orchestrator import _intercept_tool_result

RESULT = {
    "success": True,
    "group_id": "7d4faf52d9cfd2a174d90da2aa580e6f",
    "orders": 2,
    "currency": "USD",
    "totals": {"order_total": "-90008.99", "tax": "0.00", "refunds": "0.00"},
    "causes": [
        {
            "cause": "source_adjustment_not_in_netsuite",
            "label": "Solidus adjustment never reached NetSuite",
            "why": "…",
            "next_step": "fix_at_source",
            "next_label": "…",
            "orders": 2,
            "order_references": ["R290684941", "R557450535"],
            "amounts": {"order_total": "-90008.99", "tax": "0.00", "refunds": "0.00", "open_on_invoices": "413.23"},
            "facts": [{"fact": '"SKU Adjustment"', "orders": 2}],
        }
    ],
    "checked": {
        "saved_evidence": 2,
        "saved_source_orders": 2,
        "netsuite": "complete",
        "netsuite_orders": 2,
        "seconds": 1.2,
    },
}


def test_the_card_gets_the_whole_result_and_the_model_gets_no_amount():
    for name in ("transaction_ops.group_breakdown", "transaction_ops_group_breakdown"):
        event_type, event, for_model = _intercept_tool_result(name, json.dumps(RESULT))
        assert event_type == "group_breakdown" and event == RESULT
        assert "90008" not in for_model and "413.23" not in for_model and '"amounts"' not in for_model
        assert json.loads(for_model)["causes"][0]["cause"] == "source_adjustment_not_in_netsuite"


def test_a_failed_breakdown_reaches_the_model_unchanged_and_draws_no_card():
    failure = json.dumps({"success": False, "error": "Group is empty or changed; refresh the exact scoped group."})
    assert _intercept_tool_result("transaction_ops_group_breakdown", failure) == (None, None, failure)


def test_a_malformed_breakdown_draws_no_card():
    event_type, event, for_model = _intercept_tool_result("transaction_ops_group_breakdown", "{not json")
    assert event_type is None and event is None and json.loads(for_model)["success"] is False


def test_the_agent_breaks_a_group_down_before_fixing_it():
    # A mixed group sent straight to the group fix prepared nothing twice (2026-09-27/28).
    from pathlib import Path

    from app.services.chat import tool_inventory

    guidance = tool_inventory.build_mcp_execution_guidance([{"name": "ext__abc__ns_runReport"}])
    assert guidance.index("transaction_ops_group_breakdown") < guidance.index("transaction_ops_accounting_group")
    agent = Path("app/services/chat/agents/unified_agent.py").read_text()
    assert "transaction_ops_group_breakdown for the exact group" in agent
    skill = Path("app/services/chat/skills/accounting_operations/SKILL.md").read_text()
    assert "transaction_ops_group_breakdown" in skill
