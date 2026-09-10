import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp.tools import transaction_ops_tools as mod
from tests.test_transaction_ops_tools import ORDER, RUN, ctx, state  # noqa: F401


async def test_status_retains_balance_and_supported_solution_context_for_agent(ctx, state):  # noqa: F811
    state.get_run.return_value.progress_json = {
        "settlement": {"status": "difference", "operation_id": str(RUN), "approved_by": "stored-human-id"}
    }
    state.list_findings.return_value = [
        SimpleNamespace(
            report_json={
                "order_reference": ORDER,
                "comparison": {"currency": "USD", "recommended_action": "gather_evidence", "differences": []},
                "automation": {"status": "blocked", "code": "create_mapping_unproven"},
                "balance": {
                    "status": "incomplete",
                    "currency": "USD",
                    "target_currency": "USD",
                    "reason": "amount_evidence_unavailable",
                    "missing_metrics": ["refunds"],
                    "amounts": {
                        "order_total": {"source": "100.123456", "target": "100.123456", "delta": "0.000000"},
                        "tax": {"source": "10.00", "target": "10.00", "delta": "0.00"},
                        "refunds": {"source": "0.00", "target": None, "delta": None},
                    },
                },
            }
        )
    ]
    state.list_proposals = AsyncMock(
        return_value=[
            SimpleNamespace(
                id=RUN,
                order_reference=ORDER,
                action="correct_amounts",
                status="pending",
                before_json={"secret": "private"},
            )
        ]
    )
    result = await mod.execute_status({"run_id": str(RUN)}, context=ctx)
    assert result["success"]
    assert result["settlement"]["status"] == "difference"
    assert len(result["rows"]) == 3
    assert result["rows"][2][-3:] == ["0.00", None, None]
    assert result["findings"][0]["missing_metrics"] == ["refunds"]
    assert result["findings"][0]["resolution_blocker"] == "create_mapping_unproven"
    assert result["proposals"][0]["action"] == "correct_amounts"
    assert "private" not in json.dumps(result)

    from app.services.chat.agents.base_agent import _suppress_metric_value_for_llm
    from app.services.chat.orchestrator import _intercept_tool_result

    raw = json.dumps(result)
    _, _, streaming = _intercept_tool_result("transaction_ops_status", raw)
    for condensed in (streaming, _suppress_metric_value_for_llm(raw)):
        assert "100.123456" not in condensed
        assert "refunds" in condensed and "correct_amounts" in condensed
        assert json.loads(condensed)["review_url"] == result["review_url"]
        assert json.loads(condensed)["settlement"] == result["settlement"]


@pytest.mark.parametrize("bad", [10.01, "NaN"])
async def test_balance_values_are_validated_before_agent_table(ctx, state, bad):  # noqa: F811
    state.list_proposals = AsyncMock(return_value=[])
    state.list_findings.return_value = [
        SimpleNamespace(
            report_json={
                "order_reference": ORDER,
                "comparison": {"differences": []},
                "balance": {"amounts": {"order_total": {"source": bad, "target": "1", "delta": "0"}}},
            }
        )
    ]
    result = await mod.execute_status({"run_id": str(RUN)}, context=ctx)
    assert result["error"] == "invalid_stored_evidence"
