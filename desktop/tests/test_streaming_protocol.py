"""TDD tests for the streaming serve protocol (rich-pipe slice 1, Task A3).

The chat path of `serve_json_protocol`, when a request sets `"stream": true`,
drives the orchestration runner and emits each typed event as its own
newline-JSON line on the protocol stdout, terminated by `done`. Agent chatter
that escapes to `print()`/`sys.stdout` during the run must NOT appear on the
protocol stdout (stdout-isolation regression guard — the B0 #5.5 contract).

These are KEY-FREE unit-level tests: the runner is stubbed so no live agent
runs. The real end-to-end subprocess proof is `test_rich_pipe_integration.py`
(Task A4).
"""

from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "runtime"))

import sidecar  # noqa: E402  (path-augmented import)
from orchestration.events import TextEvent, DataTableEvent, DoneEvent  # noqa: E402
from suite_tools.sample_dataset import sample_dataset  # noqa: E402


class _StubAIAgent:
    """Minimal agent stub so `_ensure_agent` succeeds without a live key."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_conversation(self, user_message, **kwargs):  # pragma: no cover - runner is stubbed
        return {"final_response": user_message, "total_tokens": 0}


def _patch_agent_construction(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))
    monkeypatch.setattr(sidecar, "AIAgent", _StubAIAgent)
    monkeypatch.setattr(sidecar, "register_mcp_servers", lambda servers: list(servers.keys()))


def _chatty_runner(query, emit, *, agent, **kwargs):
    """Stub runner: prints chatter (must be isolated) and emits typed events."""
    print("Hermes status line: tool running")  # escapes to sys.stdout -> must hit stderr
    emit(TextEvent(content=f"streaming reply to: {query}"))
    emit(DataTableEvent.from_tool_result(sample_dataset()))
    emit(DoneEvent(tokens_used=7))
    return {"total_tokens": 7}


def test_streaming_run_emits_each_event_as_its_own_json_line_ending_with_done(monkeypatch, tmp_path):
    _patch_agent_construction(monkeypatch, tmp_path)
    monkeypatch.setattr(sidecar, "run_agent_stream", _chatty_runner)

    stdin = io.StringIO(json.dumps({"action": "run", "query": "show data", "stream": True}) + "\n")
    stdout = io.StringIO()
    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    lines = stdout.getvalue().strip().splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["text", "data_table", "done"], f"event order wrong: {types}"


def test_streaming_data_table_line_matches_webapp_shape(monkeypatch, tmp_path):
    _patch_agent_construction(monkeypatch, tmp_path)
    monkeypatch.setattr(sidecar, "run_agent_stream", _chatty_runner)

    stdin = io.StringIO(json.dumps({"action": "run", "query": "show data", "stream": True}) + "\n")
    stdout = io.StringIO()
    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    lines = stdout.getvalue().strip().splitlines()
    data_table_line = json.loads(next(line for line in lines if json.loads(line)["type"] == "data_table"))
    expected = sample_dataset()
    assert data_table_line == {
        "type": "data_table",
        "data": {
            "columns": expected["columns"],
            "rows": expected["rows"],
            "row_count": len(expected["rows"]),
            "query": "",
            "truncated": False,
        },
    }


def test_streaming_keeps_agent_chatter_off_protocol_stdout(monkeypatch, tmp_path):
    _patch_agent_construction(monkeypatch, tmp_path)
    monkeypatch.setattr(sidecar, "run_agent_stream", _chatty_runner)

    from contextlib import redirect_stderr, redirect_stdout

    stdin = io.StringIO(json.dumps({"action": "run", "query": "q", "stream": True}) + "\n")
    protocol_stdout = io.StringIO()
    human_stderr = io.StringIO()
    with redirect_stdout(protocol_stdout), redirect_stderr(human_stderr):
        sidecar.serve_json_protocol(stdin=stdin)

    assert "Hermes status line" not in protocol_stdout.getvalue(), "chatter leaked onto protocol stdout"
    assert "Hermes status line" in human_stderr.getvalue(), "chatter should be redirected to stderr"
    # Every protocol line is valid JSON (no chatter interleaved).
    for line in protocol_stdout.getvalue().strip().splitlines():
        json.loads(line)


def test_streaming_run_surfaces_runner_errors_as_error_json(monkeypatch, tmp_path):
    _patch_agent_construction(monkeypatch, tmp_path)

    def _boom(query, emit, *, agent, **kwargs):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(sidecar, "run_agent_stream", _boom)

    stdin = io.StringIO(json.dumps({"action": "run", "query": "q", "stream": True}) + "\n")
    stdout = io.StringIO()
    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    payloads = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert payloads[-1].get("error"), f"runner failure must surface as error JSON, got {payloads}"
    assert "runner exploded" in payloads[-1]["error"]


class _FakeHermesAgent:
    """Fake AIAgent that fires the SAME callbacks the real Hermes agent fires,
    so the REAL orchestration runner can drive it key-free (no run_agent_stream
    stubbing). Used for the runner<->serve-loop integration test below."""

    def __init__(self, **kwargs):
        self.stream_delta_callback = None
        self.tool_complete_callback = None

    def run_conversation(self, user_message, **kwargs):
        self.stream_delta_callback("Here are the sample account balances:")
        self.tool_complete_callback("c1", "sample_dataset", {}, json.dumps(sample_dataset()))
        self.stream_delta_callback(None)  # terminal None delta
        return {"final_response": "...", "total_tokens": 11}


def test_real_runner_drives_serve_loop_with_a_fake_agent(monkeypatch, tmp_path):
    # Integration: NO stubbing of run_agent_stream — the REAL runner runs through
    # the REAL serve_json_protocol, driven by a fake AIAgent that fires the
    # Hermes callbacks. Proves the runner<->sidecar wiring (callback signatures,
    # event emission, _emit_event serialization) that the per-layer unit tests
    # don't exercise together. Key-free.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))
    monkeypatch.setattr(sidecar, "register_mcp_servers", lambda servers: list(servers.keys()))
    monkeypatch.setattr(sidecar, "AIAgent", _FakeHermesAgent)
    # run_agent_stream is intentionally NOT patched — the real one runs.

    stdin = io.StringIO(json.dumps({"action": "run", "query": "show data", "stream": True}) + "\n")
    stdout = io.StringIO()
    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    payloads = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    types = [p["type"] for p in payloads]
    assert "text" in types, f"real runner must stream text; got {types}"
    assert types[-1] == "done", f"stream must terminate with done; got {types}"
    data_table = next(p for p in payloads if p["type"] == "data_table")
    expected = sample_dataset()
    assert data_table["data"]["columns"] == expected["columns"]
    assert data_table["data"]["rows"] == expected["rows"]
    assert payloads[-1]["tokens_used"] == 11


def test_non_streaming_run_keeps_legacy_blob_response(monkeypatch, tmp_path):
    # Back-compat: a request WITHOUT stream:true must still get the single
    # {"response","tokens_used"} blob (the existing Electron runAgert path).
    _patch_agent_construction(monkeypatch, tmp_path)

    stdin = io.StringIO(json.dumps({"action": "run", "query": "hello"}) + "\n")
    stdout = io.StringIO()
    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    lines = stdout.getvalue().strip().splitlines()
    assert len(lines) == 1, f"non-streaming run must emit exactly one blob line, got {lines}"
    payload = json.loads(lines[0])
    assert "response" in payload and "tokens_used" in payload
    assert "type" not in payload, "blob response must not be a typed event"
