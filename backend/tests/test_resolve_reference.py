"""B4: native Claude (Opus 5.5, high thinking) + the NetSuite MCP, in a plain tool loop.

Same task prompt, same tape and the same meters as our agent; tools are the facts-only case
file plus the Framework NetSuite MCP tools, never our card builders or evidence tools (they
return our own fix). Its writes are its proposals: recorded and answered "pending approval",
never sent.
"""

import json

import httpx

from app.services.benchmarks.resolve import reference, tape, tasks
from tests.test_resolve_bench import _anthropic, _message

EXT = "ext__" + "a" * 32 + "__"
MCP_TOOLS = [
    {"name": EXT + "ns_runCustomSuiteQL", "description": "SuiteQL", "input_schema": {"type": "object"}},
    {"name": EXT + "ns_getRecord", "description": "get", "input_schema": {"type": "object"}},
    {"name": EXT + "ns_createRecord", "description": "create", "input_schema": {"type": "object"}},
    {"name": EXT + "ns_updateRecord", "description": "update", "input_schema": {"type": "object"}},
    {"name": EXT + "ns_report_filters_app", "description": "a widget", "input_schema": {"type": "object"}},
]
CREDIT = {"recordType": "creditMemo", "data": json.dumps({"memo": "R231821517 reseller discount"})}


def _turn(*blocks, stop="tool_use", usage=None):
    body = _message(usage or {"input_tokens": 10, "output_tokens": 5})
    return ("json", {**body, "content": list(blocks), "stop_reason": stop})


def _tool_use(ident, name, tool_input):
    return {"type": "tool_use", "id": ident, "name": name, "input": tool_input}


def test_the_native_tools_are_the_case_file_and_the_netsuite_mcp_only():
    names = [t["name"] for t in reference.native_tools(MCP_TOOLS)]
    assert names[0] == reference.CASE_OPEN
    assert set(names[1:]) == {
        EXT + "ns_runCustomSuiteQL",
        EXT + "ns_getRecord",
        EXT + "ns_createRecord",
        EXT + "ns_updateRecord",
    }
    assert not any(n.startswith("transaction_ops") for n in names)


async def test_a_native_run_reads_proposes_and_answers(tmp_path, monkeypatch):
    from app.services.transaction_ops import case_file

    async def open_case(db, tenant_id, *, case_id=None, order_reference=None):
        return {"case": {"order_reference": "R231821517"}, "comparison": {"order_total": {"difference": "-674.73"}}}

    async def mcp_tools(db, tenant_id):
        return MCP_TOOLS

    monkeypatch.setattr(case_file, "open_case", open_case)
    monkeypatch.setattr(reference, "_mcp_tools", mcp_tools)
    t = tape.Tape(tmp_path / "t.jsonl")
    query = {"sqlQuery": "SELECT id FROM transaction"}
    t.put(
        tape.tape_key(EXT + "ns_runCustomSuiteQL", query, tenant_id="t"),
        EXT + "ns_runCustomSuiteQL",
        query,
        '{"data": []}',
    )
    client = _anthropic(
        [
            _turn(_tool_use("tu1", reference.CASE_OPEN, {"case_id": "case-1"})),
            _turn(_tool_use("tu2", EXT + "ns_runCustomSuiteQL", query)),
            _turn(_tool_use("tu3", EXT + "ns_createRecord", CREDIT)),
            _turn({"type": "text", "text": "Proposed a 674.73 credit memo against the invoice."}, stop="end_turn"),
        ]
    )
    monkeypatch.setattr(reference, "_client", lambda: client)
    task = tasks.Task(ref="R231821517", case_id="case-1", prompt="Resolve it.", gold=None)
    a = await reference.run_reference(task, 0, db=None, tenant_id="t", actor_id="u", tape=t, mode="replay")
    assert a.error is None and a.reply_text.startswith("Proposed")
    assert [(w.action, w.record_type) for w in a.writes] == [("create", "creditmemo")]
    assert (a.writes_are_proposals, a.writes_reached_dispatcher) == (True, 0)
    assert (a.input_tokens, a.output_tokens, a.tool_calls, a.tape_misses) == (40, 20, 3, 0)


async def test_every_request_runs_opus_with_high_thinking_and_keeps_its_thinking_blocks(tmp_path, monkeypatch):
    sent = []

    async def mcp_tools(db, tenant_id):
        return MCP_TOOLS

    thinking = {"type": "thinking", "thinking": "...", "signature": "sig"}
    answers = [
        _turn(thinking, _tool_use("tu1", EXT + "ns_createRecord", CREDIT)),
        _turn({"type": "text", "text": "Done."}, stop="end_turn"),
    ]

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=answers.pop(0)[1])

    import anthropic

    client = anthropic.AsyncAnthropic(
        api_key="x", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(reference, "_mcp_tools", mcp_tools)
    monkeypatch.setattr(reference, "_client", lambda: client)
    task = tasks.Task(ref="R1", case_id="c", prompt="Resolve it.", gold=None)
    await reference.run_reference(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="replay"
    )
    assert sent[0]["model"] == reference.MODEL and "thinking" in sent[0]
    assert sent[1]["messages"][1]["content"][0]["type"] == "thinking"  # echoed back, as tool use with thinking requires


async def test_a_run_that_never_finishes_ends_with_a_reason(tmp_path, monkeypatch):
    async def mcp_tools(db, tenant_id):
        return MCP_TOOLS

    monkeypatch.setattr(reference, "_mcp_tools", mcp_tools)
    monkeypatch.setattr(reference, "MAX_STEPS", 2)
    client = _anthropic(
        [
            _turn(_tool_use(f"tu{i}", EXT + "ns_getRecord", {"recordType": "invoice", "recordId": str(i)}))
            for i in range(2)
        ]
    )
    monkeypatch.setattr(reference, "_client", lambda: client)
    task = tasks.Task(ref="R1", case_id="c", prompt="Resolve it.", gold=None)
    a = await reference.run_reference(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="replay"
    )
    assert a.error == "max_steps (2)"


def test_run_can_drive_either_agent():
    from app.services.benchmarks.resolve.__main__ import _parser

    base = [
        "run",
        "--tasks",
        "t.json",
        "--snapshots",
        "s",
        "--tape",
        "x",
        "--out",
        "y",
        "--tenant",
        "t",
        "--model",
        "m",
        "--actor",
        "a",
    ]
    assert _parser().parse_args(base).agent == "ours"
    assert _parser().parse_args([*base, "--agent", "reference"]).agent == "reference"
