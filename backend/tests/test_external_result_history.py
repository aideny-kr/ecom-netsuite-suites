"""Successful approved reads retain bounded evidence across live/reloaded history."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.chat import ChatMessage
from app.services.chat.write_confirmation_service import build_confirmation_payload, format_external_result
from tests.test_write_confirm_orchestrator import _TENANT_ID, _USER_ID, _make_db, _make_session


def _data():
    return {
        "results": [
            {
                "title": "Python reference",
                "content": "Example paragraph. " * 2000,
                "contentUrl": "https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python",
            }
            for _ in range(10)
        ]
    }


def test_large_connected_result_keeps_source_and_labels_incomplete():
    rendered = format_external_result(_data())
    assert "Partial result preview" in rendered
    assert "https://learn.microsoft.com/" in rendered
    assert "Python reference" in rendered
    assert len(rendered) < 20500
    preview = json.loads(rendered.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert len(preview["results"]) <= 5
    assert "[truncated]" in preview["results"][0]["content"]


@pytest.mark.parametrize(
    "data",
    [
        {"content": "```\n[malicious](javascript:evil)\n```"},
        {"content": "`" * 30000},
        {"content": "\U0001f680" * 30000},
        {"rows": [[["a" * 30000] * 20] * 20] * 20},
    ],
)
def test_result_preview_is_bounded_valid_json_and_cannot_escape_fence(data):
    rendered = format_external_result(data)
    assert len(rendered) < 20500
    assert rendered.count("```") == 2
    json.loads(rendered.split("```json\n", 1)[1].rsplit("\n```", 1)[0])


@pytest.mark.parametrize("http_api", [False, True])
@pytest.mark.parametrize(
    "result,outcome",
    [(_data(), "returned"), ({"error": "refused"}, "failed"), ({"outcome_indeterminate": True}, "indeterminate")],
)
async def test_confirmed_custom_result_receipt_is_persisted_and_emitted(result, outcome, http_api):
    from app.services.chat.orchestrator import run_chat_turn

    sid = uuid.uuid4()
    tool = f"ext__{uuid.uuid4().hex}__docs_search"
    if http_api:
        tool = f"http__{uuid.uuid4().hex}__get"
    card = build_confirmation_payload(
        mutation_type="execute",
        record_type="external tool docs_search",
        tool_name=tool,
        tool_input={"query": "Python"},
        session_id=str(sid),
    )
    msg = ChatMessage(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        session_id=sid,
        role="assistant",
        content="Confirm search",
        structured_output=card.model_dump(),
    )
    db = _make_db(msg)
    execute = AsyncMock(return_value=json.dumps(result))
    with (
        patch("app.services.chat.orchestrator.execute_tool_call", execute),
        patch("app.services.chat.orchestrator.log_event", AsyncMock()),
    ):
        events = [
            e
            async for e in run_chat_turn(
                db=db,
                session=_make_session(str(sid)),
                user_message="approve",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "approve", "confirmation_id": str(msg.id)},
            )
        ]
    response = next(e["message"] for e in events if e["type"] == "message")
    saved = next(c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ChatMessage))
    assert saved.tool_calls == response["tool_calls"]
    assert response["tool_calls"][0]["duration_ms"] >= 0
    assert saved.structured_output == response["structured_output"]
    receipt = response["structured_output"]["execution_receipt"]
    assert receipt["tools"][0]["tool"] == tool
    assert receipt["tools"][0]["outcome"] == outcome
    assert receipt["tools"][0]["connector_id"] == str(uuid.UUID(tool.split("__")[1]))
    assert execute.await_args.kwargs["human_approved"] is True


@pytest.mark.parametrize(
    "outcome,summary,label",
    [
        ("returned", "Documentation about error handling and invalid input", "RETURNED"),
        (
            "indeterminate",
            "The connected service did not confirm success. Check its current state before retrying.",
            "INDETERMINATE",
        ),
        ("failed", "The connected service reported a failed request.", "FAILED"),
        ("confirmation_required", "Waiting for the signed decision", "AWAITING CONFIRMATION"),
        ("unclassified", "No explicit outcome", "OUTCOME UNCLASSIFIED"),
    ],
)
def test_follow_up_history_uses_execution_evidence_not_result_prose(outcome, summary, label):
    from app.services.chat.history_tool_trace import build_history_dicts

    history, _ = build_history_dicts(
        [
            {
                "role": "assistant",
                "content": "Connected result",
                "tool_calls": [
                    {
                        "step": 0,
                        "tool": "docs_search",
                        "params": {"query": "Python"},
                        "result_summary": summary,
                        "execution_outcome": outcome,
                    }
                ],
            }
        ],
        keep_recent=4,
    )
    trace = history[0]["content"]
    assert f"→ {label}" in trace
    assert "→ OK" not in trace
    if outcome == "returned":
        assert "FAILED" not in trace


@pytest.mark.parametrize("outcome", ["error", "failed"])
@pytest.mark.parametrize("tool,param", [("netsuite_suiteql", "query"), ("ext__abcdef__suiteql", "sqlQuery")])
def test_explicit_failure_retains_query_error_reason_without_replaying_sql(outcome, tool, param):
    from app.services.chat.history_tool_trace import render_tool_trace

    trace = render_tool_trace(
        [
            {
                "step": 0,
                "tool": tool,
                "params": {param: "SELECT t.shipcountry FROM transaction t"},
                "result_summary": "NetSuite query failed: Field 'shipcountry' NOT_EXPOSED",
                "execution_outcome": outcome,
            }
        ]
    )
    assert "FAILED: shipcountry NOT_EXPOSED" in trace
    assert "SELECT" not in trace
