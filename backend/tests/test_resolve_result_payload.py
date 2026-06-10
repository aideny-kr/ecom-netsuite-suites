# backend/tests/test_resolve_result_payload.py
"""Unit test for resolve_result_payload — the §16.1 fix: a composed report must
resolve the FULL, uncapped frozen payload from ChatMessage.tool_calls[].result_payload,
NOT the 50-row-capped Redis result cache."""

from app.services.chat.tool_call_results import resolve_payload_from_messages


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
