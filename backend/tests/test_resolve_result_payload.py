# backend/tests/test_resolve_result_payload.py
"""Unit test for resolve_result_payload — the §16.1 fix: a composed report must
resolve the FULL, uncapped frozen payload from ChatMessage.tool_calls[].result_payload,
NOT the 50-row-capped Redis result cache."""

import inspect

import pytest

from app.services.chat.tool_call_results import (
    load_conversation_tool_messages,
    resolve_payload_from_messages,
)


def _msg(tool_calls):
    return {"role": "assistant", "tool_calls": tool_calls}


def test_resolve_returns_full_uncapped_payload():
    """A source result with >50 rows must come back uncapped."""
    big_rows = [["P%d" % i, str(i)] for i in range(120)]
    messages = [
        _msg(
            [
                {
                    "tool": "netsuite.suiteql",
                    "result_id": "r1",
                    "result_payload": {
                        "kind": "table",
                        "columns": ["Period", "N"],
                        "rows": big_rows,
                        "row_count": 120,
                    },
                }
            ]
        )
    ]
    payload = resolve_payload_from_messages(messages, "r1")
    assert payload["row_count"] == 120
    assert len(payload["rows"]) == 120  # NOT capped at 50


def test_resolve_unknown_id_raises_keyerror():
    messages = [_msg([{"tool": "x", "result_id": "r1", "result_payload": {"rows": []}}])]
    try:
        resolve_payload_from_messages(messages, "missing")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown result_id")


def test_resolve_matches_synthetic_index_id():
    """Tool calls without an explicit result_id are addressable by positional id (r1, r2...)."""
    messages = [
        _msg(
            [
                {"tool": "a", "result_payload": {"rows": [["x"]], "row_count": 1}},
                {"tool": "b", "result_payload": {"rows": [["y"]], "row_count": 1}},
            ]
        )
    ]
    assert resolve_payload_from_messages(messages, "r2")["rows"] == [["y"]]


# --- Gate D (finding #18): defense-in-depth tenant filter on the resolver query ---


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingDB:
    """Captures the SQLAlchemy statement passed to execute() so the test can assert
    the WHERE clause includes a tenant_id predicate."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult()


def test_load_conversation_tool_messages_requires_tenant_id():
    """tenant_id must be a REQUIRED parameter (no default) so callers can't forget it."""
    sig = inspect.signature(load_conversation_tool_messages)
    assert "tenant_id" in sig.parameters, "load_conversation_tool_messages must accept tenant_id"
    assert sig.parameters["tenant_id"].default is inspect.Parameter.empty, (
        "tenant_id must be REQUIRED (no default) — defense-in-depth against RLS not being set"
    )


@pytest.mark.asyncio
async def test_load_conversation_tool_messages_filters_by_tenant():
    """The resolver SELECT must carry an explicit ChatMessage.tenant_id == tenant_id
    predicate (RLS may not be set on the chat tool-exec path)."""
    import uuid

    db = _CapturingDB()
    tenant_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    conv_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
    await load_conversation_tool_messages(db, conv_uuid, tenant_id=tenant_uuid)
    assert db.statements, "expected a SELECT to be executed"
    compiled = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    # The query must constrain BOTH session_id AND tenant_id (defense-in-depth).
    assert "tenant_id = " in compiled, f"tenant_id filter missing from query:\n{compiled}"
    # UUID literals render dashless; match on the hex form.
    assert tenant_uuid.hex in compiled, f"tenant_id value not bound into query:\n{compiled}"


@pytest.mark.asyncio
async def test_load_conversation_tool_messages_none_conversation_returns_empty():
    db = _CapturingDB()
    out = await load_conversation_tool_messages(db, None, tenant_id="ten-1")
    assert out == []
    assert not db.statements  # short-circuits before any SELECT
