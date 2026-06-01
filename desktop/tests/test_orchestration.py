"""TDD tests for the extraction-shaped orchestration seed (rich-pipe slice 1, A2).

The runner wraps Hermes' `run_conversation` and turns a turn into a stream of
typed events whose serialized JSON matches the webapp's `ChatStreamEvent` shapes
(`frontend/src/lib/chat-stream.ts`):

    text       -> {"type": "text", "content": "..."}
    data_table -> {"type": "data_table", "data": {columns, rows, row_count, query, truncated}}
    done       -> {"type": "done", "tokens_used": <int>}   (desktop-local terminal marker)

These tests are KEY-FREE: they drive the runner with a fake agent that fires the
exact callbacks the real Hermes agent fires (`stream_delta_callback` per text
delta, `tool_complete_callback` per tool result). The real end-to-end agent run
is Task A4 (operator-deferred — needs a live key).
"""

from __future__ import annotations

import json
import pathlib
import sys
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "runtime"))

from orchestration import runner  # noqa: E402
from orchestration.runner import _tokens_used  # noqa: E402
from orchestration.events import TextEvent, DataTableEvent, DoneEvent  # noqa: E402
from suite_tools.sample_dataset import sample_dataset, sample_dataset_handler  # noqa: E402


class _FakeHermesAgent:
    """Stands in for Hermes `AIAgent`, key-free.

    Fires the same callbacks the real agent fires during `run_conversation`:
    `stream_delta_callback(text)` per text delta (including a terminal `None`
    delta, which Hermes does at end-of-stream), and `tool_complete_callback(
    tool_call_id, tool_name, tool_args, tool_result)` after the tool runs, with
    `tool_result` being the handler's JSON-string return.
    """

    def __init__(self, tool_result: str):
        self.stream_delta_callback = None
        self.tool_complete_callback = None
        self._tool_result = tool_result
        self.last_query = None

    def run_conversation(self, user_message, **kwargs):
        self.last_query = user_message
        # Assistant streams some text, calls sample_dataset, streams a closing line.
        self.stream_delta_callback("Here are the sample account balances:")
        self.tool_complete_callback("call_1", "sample_dataset", {}, self._tool_result)
        self.stream_delta_callback("\n\nThat is the demo data.")
        self.stream_delta_callback(None)  # Hermes fires a terminal None delta
        return {
            "final_response": "Here are the sample account balances. That is the demo data.",
            "input_tokens": 18,
            "output_tokens": 24,
            "total_tokens": 42,
        }


def _run(query="show me sample data"):
    agent = _FakeHermesAgent(sample_dataset_handler({}))
    events: list = []
    result = runner.run_agent_stream(query, events.append, agent=agent)
    return agent, events, result


def test_emits_at_least_one_text_event_and_drops_the_terminal_none_delta():
    _agent, events, _ = _run()
    texts = [e for e in events if isinstance(e, TextEvent)]
    assert len(texts) >= 1, "runner must stream assistant text deltas as text events"
    assert all(t.content for t in texts), "the terminal None delta must NOT become a text event"


def test_emits_exactly_one_data_table_event_with_the_tools_columns_and_rows():
    _agent, events, _ = _run()
    tables = [e for e in events if isinstance(e, DataTableEvent)]
    assert len(tables) == 1, f"expected exactly one data_table event, got {len(tables)}"

    expected_data = sample_dataset()
    assert tables[0].to_dict() == {
        "type": "data_table",
        "data": {
            "columns": expected_data["columns"],
            "rows": expected_data["rows"],
            "row_count": len(expected_data["rows"]),
            "query": "",
            "truncated": False,
        },
    }


def test_terminates_with_a_done_event_carrying_int_tokens_used():
    _agent, events, _ = _run()
    assert isinstance(events[-1], DoneEvent), f"last event must be done, got {events[-1]!r}"
    assert isinstance(events[-1].tokens_used, int)
    assert events[-1].tokens_used == 42  # total_tokens from the agent result


def test_event_order_is_text_then_data_table_then_done():
    _agent, events, _ = _run()
    first_text = next(i for i, e in enumerate(events) if isinstance(e, TextEvent))
    table_idx = next(i for i, e in enumerate(events) if isinstance(e, DataTableEvent))
    assert first_text < table_idx, "at least one text event must precede the data_table"
    assert isinstance(events[-1], DoneEvent), "done must be the terminal event"


def test_runner_returns_the_underlying_conversation_result():
    _agent, _events, result = _run()
    assert isinstance(result, dict)
    assert result.get("total_tokens") == 42


def test_runner_passes_the_query_through_to_run_conversation():
    agent, _events, _ = _run(query="please show the demo table")
    assert agent.last_query == "please show the demo table"


def test_non_sample_dataset_tool_results_do_not_become_data_tables():
    # The interception is by tool name — an unrelated tool result must be ignored.
    agent = _FakeHermesAgent('{"columns": ["x"], "rows": [[1]]}')

    def _run_other(self, user_message, **kwargs):
        self.stream_delta_callback("working")
        self.tool_complete_callback("c2", "some_other_tool", {}, self._tool_result)
        return {"total_tokens": 5}

    agent.run_conversation = _run_other.__get__(agent, _FakeHermesAgent)
    events: list = []
    runner.run_agent_stream("q", events.append, agent=agent)
    assert not any(isinstance(e, DataTableEvent) for e in events)


@pytest.mark.parametrize(
    ("bad_result", "why"),
    [
        ("this is not json at all", "malformed JSON"),
        (json.dumps({"rows": [[1]]}), "missing columns key"),
        (json.dumps({"columns": ["x"]}), "missing rows key"),
        (json.dumps({"columns": 5, "rows": [[1]]}), "columns is not a list"),
        (json.dumps({"columns": ["x"], "rows": "nope"}), "rows is not a list"),
        (json.dumps(None), "tool result is JSON null"),
    ],
)
def test_malformed_tool_result_skips_data_table_but_still_streams(bad_result, why):
    # The interception must tolerate a malformed tool result: skip the
    # data_table (no crash), and the assistant's text + done still stream.
    agent = _FakeHermesAgent(bad_result)
    events: list = []
    runner.run_agent_stream("q", events.append, agent=agent)

    assert not any(isinstance(e, DataTableEvent) for e in events), f"should skip data_table when {why}"
    assert any(isinstance(e, TextEvent) for e in events), f"text must still stream when {why}"
    assert isinstance(events[-1], DoneEvent), f"done must still terminate when {why}"


def test_tokens_used_prefers_total_then_falls_back_then_defaults_zero():
    assert _tokens_used({"total_tokens": 9}) == 9
    assert _tokens_used({"input_tokens": 5, "output_tokens": 7}) == 12
    # Non-int / non-dict / missing all counters → 0 (the done event never carries null).
    assert _tokens_used({"total_tokens": "42"}) == 0
    assert _tokens_used({"input_tokens": None, "output_tokens": 3}) == 3
    assert _tokens_used("not a dict") == 0
    assert _tokens_used({}) == 0


def test_runner_is_extraction_shaped_no_transport_imports():
    # Guardrail: the orchestration core must be free of Electron/IPC/transport
    # coupling so the later packages/agent extraction RELOCATES it unchanged.
    src = pathlib.Path(runner.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import sidecar", "from sidecar",      # the desktop transport adapter
        "electron", "ipc",                       # Electron / IPC
        "import run_agent", "from run_agent",  # the Hermes agent lib (must be injected, not imported)
        "sys.stdout", "print(",                  # any direct stdout transport
    )
    for token in forbidden:
        assert token not in src, (
            f"runner.py must stay transport-agnostic; found forbidden token {token!r}"
        )
