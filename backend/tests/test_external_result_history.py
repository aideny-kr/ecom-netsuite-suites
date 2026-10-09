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


@pytest.mark.parametrize("result,outcome", [(_data(), "returned"), ({"error": "refused"}, "failed")])
async def test_confirmed_custom_result_receipt_is_persisted_and_emitted(result, outcome):
    from app.services.chat.orchestrator import run_chat_turn

    sid = uuid.uuid4()
    tool = f"ext__{uuid.uuid4().hex}__docs_search"
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
    assert saved.structured_output == response["structured_output"]
    receipt = response["structured_output"]["execution_receipt"]
    assert receipt["tools"][0]["tool"] == tool
    assert receipt["tools"][0]["outcome"] == outcome
    assert receipt["tools"][0]["connector_id"] == str(uuid.UUID(tool.split("__")[1]))
    assert execute.await_args.kwargs["human_approved"] is True
