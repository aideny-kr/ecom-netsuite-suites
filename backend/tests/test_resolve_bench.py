"""Resolve benchmark harness (spec 2026-10-01 §7, block B3): tasks, the read tape, graders, report.

Synthetic order references only: real Framework cases and labels never enter git.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.benchmarks.resolve import graders, report, tape, tasks
from app.services.benchmarks.resolve.graders import Attempt, Proposal
from app.services.benchmarks.resolve.meter import ModelMeter
from app.services.benchmarks.resolve.ours import attempt_from_run
from app.services.chat.agents.base_agent import AgentResult
from app.services.chat.llm_adapter import TokenUsage

EXT = "ext__" + "a" * 32 + "__"
REFS = [f"R10000000{i}" for i in range(8)]


def _label(ref, **over):
    label = {
        "order_reference": ref,
        "case_id": f"case-{ref}",
        "diagnosis": "credited",
        "action": "explain_close",
        "confidence": "sure",
        "evidence": "CM covers the adjustment",
        "change": None,
    }
    label.update(over)
    return label


def _write_bench(tmp_path, labels=None, *, layout="flat"):
    (tmp_path / "tasks.json").write_text(json.dumps([{"ref": r, "case_id": f"case-{r}"} for r in REFS]))
    root = tmp_path / "labels"
    target = root / "labels" if layout == "artifact" else root
    target.mkdir(parents=True)
    for ref in REFS:
        label = (labels or {}).get(ref, _label(ref))
        if label is not None:
            (target / f"{ref}.json").write_text(json.dumps(label))
    return tmp_path / "tasks.json", root


# --- tasks: split, gold, validation -------------------------------------------------


def test_a_quarter_is_held_out_by_hash_and_the_split_is_stable():
    held = tasks.held_out_refs(REFS)
    expected = sorted(REFS, key=lambda r: hashlib.sha256(f"resolve-bench:{r}".encode()).hexdigest())[:2]
    assert held == frozenset(expected)
    assert tasks.held_out_refs(list(reversed(REFS))) == held


def test_held_in_never_returns_held_out_gold(tmp_path):
    tasks_path, labels = _write_bench(tmp_path)
    held = tasks.held_out_refs(REFS)
    loaded = tasks.load_tasks(tasks_path, labels)
    assert len(loaded) == 6 and not {t.ref for t in loaded} & held
    assert {t.ref for t in tasks.load_tasks(tasks_path, labels, split="held_out")} == held


def test_artifact_export_layout_and_amounts_parse(tmp_path):
    change = {"record": "Credit memo", "created_from": "Invoice", "amount": "$1,234.50", "item": "1471", "memo": ""}
    labels = {r: _label(r, diagnosis="needs_credit_memo", action="create", change=change) for r in REFS}
    tasks_path, root = _write_bench(tmp_path, labels, layout="artifact")
    gold = tasks.load_tasks(tasks_path, root, split="all")[0].gold
    assert gold.change.amount == Decimal("1234.50") and gold.change.record == "Credit memo"


def test_unsure_and_missing_labels_are_not_gold(tmp_path):
    labels = {REFS[0]: _label(REFS[0], confidence="unsure"), REFS[1]: None}
    tasks_path, root = _write_bench(tmp_path, labels)
    refs = {t.ref for t in tasks.load_tasks(tasks_path, root, split="all")}
    assert REFS[0] not in refs and REFS[1] not in refs and len(refs) == 6


@pytest.mark.parametrize(
    "bad",
    [
        {"diagnosis": "bogus"},
        {"action": "post_it"},
        {"action": "create", "change": None},
        {"action": "create", "change": {"record": "Credit memo", "amount": "four"}},
        {"order_reference": "R999"},
    ],
)
def test_an_invalid_label_fails_loudly_naming_the_order(tmp_path, bad):
    labels = {REFS[0]: _label(REFS[0], **bad)}
    tasks_path, root = _write_bench(tmp_path, labels)
    with pytest.raises(ValueError, match=REFS[0]):
        tasks.load_tasks(tasks_path, root, split="all")


# --- tape: what may run, record once, replay after ----------------------------------


@pytest.mark.parametrize(
    "name, kind",
    [
        ("netsuite_suiteql", "read"),
        (EXT + "ns_runCustomSuiteQL", "read"),
        (EXT + "ns_getRecord", "read"),
        ("transaction_ops_accounting_evidence", "read"),
        (EXT + "ns_createRecord", "write"),
        (EXT + "ns_updateRecord", "write"),
        ("transaction_ops_accounting_amendment_apply", "write"),
        ("transaction_ops_run", "refused"),
        ("workspace_propose_patch", "refused"),
        ("something_new", "refused"),
    ],
)
def test_tools_are_read_write_or_refused(name, kind):
    assert tape.classify(name) == kind


class _Live:
    def __init__(self):
        self.calls = []

    async def __call__(self, tool_name, tool_input, **kwargs):
        self.calls.append(tool_name)
        return json.dumps({"rows": [{"live": tool_name}]})


async def test_record_once_then_replay_from_disk(tmp_path):
    live = _Live()
    recorder = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    first = await recorder("netsuite_suiteql", {"query": "SELECT 1"})
    again = await recorder("netsuite_suiteql", {"query": "SELECT 1"})
    assert first == again and live.calls == ["netsuite_suiteql"]

    player = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    assert await player("netsuite_suiteql", {"query": "SELECT 1"}) == first
    missed = json.loads(await player("netsuite_suiteql", {"query": "SELECT 2"}))
    assert missed["not_recorded"] is True and player.misses == 1


async def test_writes_and_refused_tools_never_run_in_any_mode(tmp_path):
    live = _Live()
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    wrote = json.loads(await d(EXT + "ns_createRecord", {"recordType": "creditmemo"}))
    refused = json.loads(await d("transaction_ops_run", {}))
    assert live.calls == [] and wrote["benchmark"] and refused["benchmark"]
    assert d.writes == [EXT + "ns_createRecord"] and d.refused == ["transaction_ops_run"]


async def test_installed_routes_every_tool_call_through_the_tape(tmp_path):
    from app.services.chat import tools

    t = tape.Tape(tmp_path / "t.jsonl")
    t.put(
        tape.tape_key("netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t"),
        "netsuite_suiteql",
        {"query": "SELECT 1"},
        '{"ok": 1}',
    )
    d = tape.TapedDispatcher(t, mode="replay")
    with tape.installed(d):
        got = await tools.execute_tool_call(
            "netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t", actor_id="a", correlation_id="c", db=None
        )
    assert got == '{"ok": 1}'
    assert tools._execute_tool_call_once is not d  # restored on exit


# --- graders ------------------------------------------------------------------------


CARD = {
    "type": "write_confirmation",
    "mutation_type": "create",
    "record_type": "creditmemo",
    "proposed_fields": {"createdFrom": {"id": "77", "refName": "Invoice #INV1"}, "memo": "R100000000 Fix"},
    "proposed_lines": [{"item": {"id": "1471", "refName": "Sales Adjustments"}, "amount": -4.82}],
    "tool_name": EXT + "ns_createRecord",
    "tool_input": {},
    "confirmation_token": "tok",
}


def test_a_card_becomes_a_proposal():
    p = graders.proposal_from_card(CARD)
    assert (p.action, p.record, p.created_from, p.amount) == ("create", "Credit memo", "Invoice", Decimal("4.82"))
    assert "1471" in p.item_text and p.memo == "R100000000 Fix"


def _gold(action="create", diagnosis="needs_credit_memo", **change):
    c = {
        "record": "Credit memo",
        "created_from": "Invoice",
        "amount": Decimal("4.82"),
        "item": "1471 → 40050",
        "memo": "",
    }
    c.update(change)
    return tasks.Gold(
        diagnosis=diagnosis,
        action=action,
        change=tasks.Change(**c) if action in ("create", "update") else None,
        evidence="",
    )


def _attempt(**over):
    a = {
        "reply_text": "Prepared the credit memo for approval.",
        "proposals": [
            Proposal(
                action="create",
                record="Credit memo",
                created_from="Invoice",
                amount=Decimal("4.82"),
                item_text="1471 Sales Adjustments 40050",
                memo="",
            )
        ],
        "resolution": {"diagnosis": "needs_credit_memo", "action": "create"},
    }
    a.update(over)
    return Attempt(**a)


def _task(gold):
    return tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=gold)


def test_the_exact_change_in_one_card_passes():
    g = graders.grade(_task(_gold()), _attempt())
    assert g.outcome_ok and g.payload_ok and g.safety_violations == 0


@pytest.mark.parametrize(
    "proposal, field",
    [
        ({"amount": Decimal("4.83")}, "amount"),
        ({"record": "Journal entry"}, "record"),
        ({"created_from": "Sales order"}, "created_from"),
        ({"item_text": "1471 Sales Adjustments"}, "item"),
    ],
)
def test_a_wrong_payload_fails_and_names_the_field(proposal, field):
    p = Proposal(**{**_attempt().proposals[0].__dict__, **proposal})
    g = graders.grade(_task(_gold()), _attempt(proposals=[p]))
    assert not g.outcome_ok and field in g.payload_diff


def test_two_cards_for_one_change_fail():
    a = _attempt()
    assert not graders.grade(_task(_gold()), _attempt(proposals=a.proposals * 2)).action_ok


def test_explain_close_passes_only_without_a_card():
    gold = _gold(action="explain_close", diagnosis="credited")
    res = {"diagnosis": "credited", "action": "explain_close"}
    assert graders.grade(_task(gold), _attempt(proposals=[], resolution=res)).outcome_ok
    assert not graders.grade(_task(gold), _attempt(resolution=res)).outcome_ok


def test_without_a_declared_diagnosis_the_interpreter_is_used_and_named():
    a = _attempt(resolution=None)
    assert not graders.grade(_task(_gold()), a).outcome_ok
    g = graders.grade(_task(_gold()), a, interpret=lambda text: {"diagnosis": "needs_credit_memo", "action": "create"})
    assert g.outcome_ok and g.resolution_source == "interpreted"


def test_a_write_that_reached_the_dispatcher_is_a_safety_violation_and_fails():
    g = graders.grade(_task(_gold()), _attempt(writes_reached_dispatcher=1))
    assert g.safety_violations == 1 and not g.outcome_ok


def test_brevity_counts_words_and_amounts_the_model_wrote():
    g = graders.grade(
        _task(_gold()), _attempt(reply_text="The credit memo for $4.82 and 1,200.00 fixes R100000000 (2 lines).")
    )
    assert g.words == 11 and g.model_amounts == ["$4.82", "1,200.00"]


# --- report -------------------------------------------------------------------------


def test_pass_at_1_and_pass_hat_3():
    rows = [{"ref": "A", "outcome_ok": True}] * 3 + [{"ref": "B", "outcome_ok": ok} for ok in (True, False, False)]
    s = report.summarize(rows, trials=3)
    assert s["g1_pass_at_1"] == pytest.approx((1 + 1 / 3) / 2) and s["g1_pass_hat_k"] == 0.5


async def test_three_tasks_run_end_to_end_and_persist(tmp_path):
    gold = _gold()
    bench = [tasks.Task(ref=f"R10000000{i}", case_id=f"c{i}", prompt="p", gold=gold) for i in range(3)]

    async def agent(task, trial):
        if task.ref.endswith("2") and trial == 1:
            return _attempt(tape_misses=1, proposals=[])
        return _attempt(input_tokens=1000, output_tokens=100, tool_calls=4, wall_ms=900)

    out = tmp_path / "run.json"
    summary = await report.run(bench, agent, trials=3, out_path=out, meta={"agent": "scripted"})
    saved = json.loads(out.read_text())
    assert len(saved["trials"]) == 9 and saved["meta"]["agent"] == "scripted"
    assert summary["g1_pass_at_1"] == pytest.approx(8 / 9)
    assert summary["environment_incomplete_trials"] == 1 and summary["comparable"] is False


def _message(usage):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }


def _stream_body(input_tokens, output_tokens):
    start = {**_message({"input_tokens": input_tokens, "output_tokens": 1}), "content": [], "stop_reason": None}
    events = [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in events)


def _anthropic(responses):
    """A real Anthropic SDK client whose HTTP answers come from `responses`, in order."""
    import anthropic
    import httpx

    queue = list(responses)

    def handler(request):
        kind, body = queue.pop(0)
        if kind == "stream":
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=body)

    return anthropic.AsyncAnthropic(api_key="x", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def model_call(client):
    await client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}])


# --- our agent: the run mapped onto an attempt ---------------------------------------


def test_our_agent_run_becomes_an_attempt(tmp_path):
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    d.misses = 2
    result = AgentResult(
        success=True,
        data="Prepared a credit memo for approval.",
        tool_calls_log=[{"tool_name": "netsuite_suiteql"}] * 3,
        tokens_used=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=7),
    )
    events = [("text", "server note $4.82"), ("confirmation_required", CARD), ("response", result)]
    a = attempt_from_run(events, d, wall_ms=1234, meter=ModelMeter(input_tokens=10, output_tokens=5, cache_tokens=7))
    assert a.reply_text == "Prepared a credit memo for approval."  # the model's words, not server-written card text
    assert [p.record for p in a.proposals] == ["Credit memo"] and a.resolution is None
    assert (a.tool_calls, a.input_tokens, a.output_tokens, a.cache_tokens, a.tape_misses) == (3, 10, 5, 7, 2)


# --- command line --------------------------------------------------------------------


def test_split_command_prints_counts_only(tmp_path, capsys):
    from app.services.benchmarks.resolve.__main__ import main

    tasks_path, _ = _write_bench(tmp_path)
    assert main(["split", "--tasks", str(tasks_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"tasks": 8, "held_in": 6, "held_out": 2}


def test_run_refuses_a_results_file_inside_the_repository(tmp_path):
    from app.services.benchmarks.resolve.__main__ import main

    tasks_path, labels = _write_bench(tmp_path)
    inside = __file__  # any path in the checkout
    with pytest.raises(SystemExit, match="outside the repository"):
        main(
            [
                "run",
                "--tasks",
                str(tasks_path),
                "--labels",
                str(labels),
                "--tape",
                str(tmp_path / "t.jsonl"),
                "--out",
                inside + ".json",
                "--tenant",
                "00000000-0000-0000-0000-000000000000",
                "--model",
                "m",
                "--actor",
                "00000000-0000-0000-0000-000000000000",
            ]
        )


def test_the_held_out_count_rounds_like_the_labelling_sheet():
    assert len(tasks.held_out_refs([f"R{i}" for i in range(14)])) == round(14 * 0.25) == 4


def test_both_turns_are_charged_when_the_agent_asked_for_the_source(tmp_path):
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    asked = AgentResult(
        success=True, data="Which data source?", tool_calls_log=[], tokens_used=TokenUsage(input_tokens=4)
    )
    answered = AgentResult(
        success=True, data="Done.", tool_calls_log=[{"tool_name": "x"}], tokens_used=TokenUsage(input_tokens=6)
    )
    a = attempt_from_run([("response", asked), ("response", answered)], d, wall_ms=1)
    assert (a.reply_text, a.tool_calls) == ("Done.", 1)


async def test_run_ours_drives_the_agent_through_the_tape(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.benchmarks import agent_runner
    from app.services.benchmarks.resolve import ours
    from app.services.chat import tools

    t = tape.Tape(tmp_path / "t.jsonl")
    t.put(
        tape.tape_key("netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t"),
        "netsuite_suiteql",
        {"query": "SELECT 1"},
        '{"ok": 1}',
    )
    seen, seen_actors = [], []
    llm = _anthropic([("json", _message({"input_tokens": 1})), ("json", _message({"input_tokens": 2}))])

    class FakeAgent:
        turns = 0

        def __init__(self, **kwargs):
            seen_actors.append(kwargs["user_id"])

        async def run_streaming(self, *, task, context, db, adapter, model, conversation_history):
            FakeAgent.turns += 1
            await model_call(llm)
            if FakeAgent.turns == 1:
                yield (
                    "response",
                    AgentResult(success=True, data="Which data source?", tokens_used=TokenUsage(input_tokens=1)),
                )
                return
            kw = dict(tenant_id="t", actor_id="a", correlation_id="c", db=None)
            seen.append(await tools.execute_tool_call("netsuite_suiteql", {"query": "SELECT 1"}, **kw))
            seen.append(await tools.execute_tool_call(EXT + "ns_createRecord", {"recordType": "creditmemo"}, **kw))
            yield "confirmation_required", CARD
            yield "response", AgentResult(success=True, data="Prepared it.", tokens_used=TokenUsage(input_tokens=2))

    async def _none(*args, **kwargs):
        return None

    async def _context(**kwargs):
        return {}

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(agent_runner, "_build_adapter", lambda **kwargs: object())
    monkeypatch.setattr(agent_runner, "get_active_metadata", _none)
    monkeypatch.setattr(agent_runner, "_load_tenant_config", _none)
    monkeypatch.setattr(agent_runner, "_assemble_context", _context)
    monkeypatch.setattr(agent_runner, "UnifiedAgent", FakeAgent)
    monkeypatch.setattr(agent_runner, "_asks_for_source", lambda result: result is not None and "source" in result.data)

    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(task, 0, db=None, tenant_id="t", actor_id="u", tape=t, mode="replay", model="m")
    assert seen[0] == '{"ok": 1}' and json.loads(seen[1])["benchmark"] is True
    assert a.writes_reached_dispatcher == 1 and [p.record for p in a.proposals] == ["Credit memo"]
    assert (a.reply_text, a.input_tokens, a.error) == ("Prepared it.", 3, None)
    assert seen_actors == ["u", "u"]  # both turns act as the given tenant user


# --- review round 1 (gpt-6-astra on 71b274ca) ------------------------------------------


def test_f7_tape_entries_are_bound_to_the_tenant_and_the_connector():
    q = {"sqlQuery": "SELECT id FROM transaction"}
    assert tape.tape_key(EXT + "ns_runCustomSuiteQL", q, tenant_id="t1") != tape.tape_key(
        EXT + "ns_runCustomSuiteQL", q, tenant_id="t2"
    )
    other = "ext__" + "b" * 32 + "__ns_runCustomSuiteQL"
    assert tape.tape_key(EXT + "ns_runCustomSuiteQL", q, tenant_id="t1") != tape.tape_key(other, q, tenant_id="t1")


@pytest.mark.parametrize(
    "name, kind",
    [
        ("transaction_ops_accounting_group", "read"),
        ("transaction_ops_propose_credit_reallocation", "read"),  # reads, then prepares a card; no writes
        ("transaction_ops.propose_credit_reallocation", "read"),
        ("netsuite_refresh_metadata", "refused"),
        ("tenant_save_learned_rule", "refused"),
        ("present_result", "local"),
    ],
)
def test_f6_model_names_and_registry_names_classify_alike(name, kind):
    assert tape.classify(name) == kind


def test_f6_every_allow_listed_name_is_a_real_tool():
    from app.mcp.registry import TOOL_REGISTRY

    unknown = {n for n in tape.READ_TOOLS | tape.LOCAL_TOOLS if n not in TOOL_REGISTRY and n not in tape.AGENT_TOOLS}
    assert unknown == set()


class _DB:
    def __init__(self):
        self.info = {}


async def test_f2_environment_errors_are_never_taped(tmp_path):
    live = _Live()

    async def unauthorized(tool_name, tool_input, **kwargs):
        live.calls.append(tool_name)
        return json.dumps({"error": "actor_unavailable"})

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=unauthorized)
    await d("transaction_ops_accounting_evidence", {"case_id": "c"}, tenant_id="t")
    await d("transaction_ops_accounting_evidence", {"case_id": "c"}, tenant_id="t")
    assert live.calls == ["transaction_ops_accounting_evidence"] * 2 and d.environment_errors == 2
    assert not (tmp_path / "t.jsonl").exists() or (tmp_path / "t.jsonl").read_text() == ""


SALES_CREDIT_CARD = {
    "type": "write_confirmation",
    "mutation_type": "create",
    "record_type": "creditmemo",
    "proposed_fields": {
        "entity": {"id": "5"},
        "account": {"id": "1471"},
        "memo": "R100000000 Fix Order Status",
        "item": {"items": [{"item": {"id": "40050"}, "rate": -4.82, "amount": -4.82, "isTaxable": False}]},
        "apply": {"items": [{"doc": {"id": "77"}, "apply": True, "amount": 4.82}]},
    },
    "proposed_lines": [],
    "accounting_review": {
        "kind": "sales_adjustment_credit",
        "invoice_id": "77",
        "expected_after": {"credit_total": "-4.82"},
    },
    "tool_name": EXT + "ns_createRecord",
    "tool_input": {},
    "confirmation_token": "tok2",
}


def test_f4_a_real_sales_credit_card_is_read_from_its_sublists():
    p = graders.proposal_from_card(SALES_CREDIT_CARD)
    assert (p.action, p.record, p.created_from, p.amount) == ("create", "Credit memo", "Invoice", Decimal("4.82"))
    assert "40050" in p.item_text and "1471" in p.item_text
    g = graders.grade(_task(_gold(item="1471 → 40050")), _attempt(proposals=[p]))
    assert g.payload_ok and g.outcome_ok


def test_f4_update_amounts_are_not_graded_from_the_card():
    card = {**SALES_CREDIT_CARD, "mutation_type": "update", "accounting_review": {"kind": "credit_tax_reallocation"}}
    p = graders.proposal_from_card(card)
    g = graders.grade(
        _task(_gold(action="update", diagnosis="netsuite_wrong_other", item="", created_from="")),
        _attempt(proposals=[p], resolution={"diagnosis": "netsuite_wrong_other", "action": "update"}),
    )
    assert g.payload_ok and "amount" not in g.payload_diff and g.amount_graded is False


@pytest.mark.parametrize(
    "origin, expected",
    [({"id": "77"}, "Invoice"), ({"id": "99"}, "unknown"), ({"id": "1", "refName": "invoice #INV9"}, "Invoice")],
)
def test_f5_created_from_is_typed_by_id_and_unknown_is_not_none(origin, expected):
    card = {
        **CARD,
        "proposed_fields": {**CARD["proposed_fields"], "createdFrom": origin},
        "accounting_review": {"invoice_id": "77"},
    }
    p = graders.proposal_from_card(card)
    assert p.created_from == expected
    if expected == "unknown":
        for gold_origin in ("Invoice", "None"):
            assert (
                "created_from"
                in graders.grade(_task(_gold(created_from=gold_origin)), _attempt(proposals=[p])).payload_diff
            )


def test_f9_held_in_never_reads_held_out_label_files(tmp_path):
    tasks_path, root = _write_bench(tmp_path)
    for ref in tasks.held_out_refs(REFS):
        (root / f"{ref}.json").write_text("{not json")
    assert len(tasks.load_tasks(tasks_path, root)) == 6


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "create", "change": {"record": "Credit memo", "amount": "NaN"}},
        {"action": "create", "change": {"record": "Credit memo", "amount": "Infinity"}},
        {"action": "create", "change": {"record": "Credit memo", "amount": "1", "created_from": "Bogus"}},
        {"confidence": "maybe"},
    ],
)
def test_f10_more_invalid_labels_fail_loudly(tmp_path, bad):
    tasks_path, root = _write_bench(tmp_path, {REFS[0]: _label(REFS[0], **bad)})
    with pytest.raises(ValueError, match=REFS[0]):
        tasks.load_tasks(tasks_path, root, split="all")


def test_f11_server_written_approval_text_is_not_the_models(tmp_path):
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    note = "Approve the credit memo for $4.82 against INV1."
    result = AgentResult(success=True, data=note, tokens_used=TokenUsage())
    a = attempt_from_run([("text", "\n\n" + note), ("confirmation_required", CARD), ("response", result)], d, wall_ms=1)
    assert a.reply_text == "" and len(a.proposals) == 1


async def test_f1_the_runner_interprets_an_undeclared_reply():
    gold = _gold()

    async def agent(task, trial):
        return _attempt(resolution=None)

    async def interpret(text):
        return {"diagnosis": "needs_credit_memo", "action": "create"}

    bench = [tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=gold)]
    assert (await report.run(bench, agent, trials=1))["g1_pass_at_1"] == 0
    assert (await report.run(bench, agent, trials=1, interpret=interpret))["g1_pass_at_1"] == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"diagnosis": "credited", "action": "explain_close"}', {"diagnosis": "credited", "action": "explain_close"}),
        ('{"diagnosis": "made_up", "action": "explain_close"}', ValueError),
        ("not json", ValueError),
        ('{"diagnosis": null, "action": null}', None),
    ],
)
def test_f1_the_interpreter_accepts_only_the_label_vocabularies(raw, expected):
    """Round 6: outside-vocabulary or malformed output is the grader's failure (raises), not
    the agent's; only a clean null reads as no conclusion."""
    from app.services.benchmarks.resolve.interpret import parse_interpretation

    if expected is ValueError:
        with pytest.raises(ValueError):
            parse_interpretation(raw)
    else:
        assert parse_interpretation(raw) == expected


def test_f12_summary_counts_trials_with_amounts():
    rows = [
        {"ref": "A", "outcome_ok": True, "model_amounts": ["$1.00", "2.00"]},
        {"ref": "A", "outcome_ok": True, "model_amounts": []},
    ]
    s = report.summarize(rows, trials=2)
    assert (s["g4_model_amounts"], s["g4_trials_with_amounts"]) == (2, 1)


# --- review round 2: nothing leaves the process in replay; stateful reads stay live ------


async def test_r2_a_read_that_leaves_session_state_is_live_in_record_and_unreplayable_in_replay(tmp_path):
    calls = []

    async def live(tool_name, tool_input, **kwargs):
        calls.append(tool_name)
        kwargs["db"].info["accounting_correction_candidate"] = {"observed_at": "now"}
        return '{"ok": 1}'

    recorder = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    for _ in range(2):
        await recorder("transaction_ops_accounting_evidence", {"case_id": "c"}, tenant_id="t", db=_DB())
    assert len(calls) == 2  # a stateful read is never served from the tape, so its state is always fresh

    player = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    db = _DB()
    assert await player("transaction_ops_accounting_evidence", {"case_id": "c"}, tenant_id="t", db=db) == '{"ok": 1}'
    assert db.info == {} and player.unreplayable == 1  # stale state is never restored; the trial is not comparable


async def test_r2_replay_blocks_every_outbound_host_but_the_model(tmp_path):
    import httpx
    import requests

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d, allow_hosts={"api.anthropic.com"}):
        async with httpx.AsyncClient() as client:
            with pytest.raises(tape.LiveNetworkBlockedError):
                await client.get("https://1234567.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql")
        with pytest.raises(tape.LiveNetworkBlockedError):
            httpx.Client().get("https://solidus.example.com/api/orders/R1")
        with pytest.raises(tape.LiveNetworkBlockedError):
            requests.Session().get("https://bigquery.googleapis.com/bigquery/v2/projects/p/queries")
        allowed = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        assert tape._allowed(allowed.url.host, {"api.anthropic.com"})
    assert d.network_blocked == 3
    assert httpx.AsyncClient.send is not None and not getattr(httpx.AsyncClient.send, "_bench_guard", False)


async def test_r2_netsuite_token_refresh_cannot_leave_a_replay(tmp_path):
    from app.services import netsuite_oauth_service as oauth

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d):
        with pytest.raises(tape.LiveNetworkBlockedError):
            await oauth.refresh_tokens("1234567", "token")
    assert d.network_blocked == 1


@pytest.mark.parametrize(
    "tool_input, kind", [({"result_id": "r1", "row_field": "a"}, "local"), ({"query": "SELECT 1"}, "read")]
)
def test_r2_pivot_is_local_only_over_an_earlier_result(tool_input, kind):
    assert tape.classify("pivot_query_result", tool_input) == kind


@pytest.mark.parametrize(
    "body",
    [
        {"error": "Accounting case or scoped configuration unavailable.", "reason": "actor_unavailable"},
        {"error": "Accounting case or scoped configuration unavailable.", "reason": "upstream_http_429"},
        {"error": "Accounting case or scoped configuration unavailable.", "reason": "read_timeout"},
        {"error": "x", "detail": "permission_denied"},
    ],
)
def test_r2_environment_errors_are_found_in_any_reason_field(body):
    assert tape._environment_error(json.dumps(body))


def test_r2_a_server_prepared_card_is_interpreted_from_what_the_person_saw(tmp_path):
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    note = "This order needs a credit memo for the unbooked adjustment. Approve below."
    result = AgentResult(success=True, data=note, tokens_used=TokenUsage())
    a = attempt_from_run([("text", "\n\n" + note), ("confirmation_required", CARD), ("response", result)], d, wall_ms=1)
    assert a.reply_text == "" and note in a.shown_text


async def test_r2_the_runner_interprets_the_shown_text_and_survives_interpreter_failure(tmp_path):
    gold = _gold()
    bench = [tasks.Task(ref=f"R10000000{i}", case_id="c", prompt="p", gold=gold) for i in range(2)]
    seen = []

    async def agent(task, trial):
        return _attempt(resolution=None, reply_text="", shown_text=f"needs a credit memo {task.ref}")

    async def interpret(text):
        seen.append(text)
        if text.endswith("1"):
            raise RuntimeError("provider down")
        return {"diagnosis": "needs_credit_memo", "action": "create"}

    out = tmp_path / "run.json"
    summary = await report.run(bench, agent, trials=1, out_path=out, interpret=interpret)
    rows = json.loads(out.read_text())["trials"]
    assert seen == ["needs a credit memo R100000000", "needs a credit memo R100000001"]
    assert summary["g1_pass_at_1"] == 0.5 and rows[1]["interpret_error"] == "RuntimeError: provider down"


async def test_r2_results_are_saved_after_every_trial(tmp_path):
    out = tmp_path / "run.json"
    bench = [tasks.Task(ref=f"R10000000{i}", case_id="c", prompt="p", gold=_gold()) for i in range(2)]

    async def agent(task, trial):
        if task.ref.endswith("1"):
            raise KeyboardInterrupt
        return _attempt()

    with pytest.raises(KeyboardInterrupt):
        await report.run(bench, agent, trials=1, out_path=out)
    assert len(json.loads(out.read_text())["trials"]) == 1


def test_r2_normalized_apply_lines_type_the_origin():
    card = {
        **SALES_CREDIT_CARD,
        "proposed_fields": {"item": {"items": SALES_CREDIT_CARD["proposed_fields"]["item"]["items"]}},
        "proposed_lines": [
            {"item": {"id": "40050"}, "amount": -4.82},
            {"doc": {"id": "77"}, "apply": True, "amount": 4.82},
        ],
    }
    assert graders.proposal_from_card(card).created_from == "Invoice"


def test_r2_summary_has_the_median_amount_count():
    rows = [
        {"ref": "A", "outcome_ok": True, "model_amounts": ["$1.00", "2.00"]},
        {"ref": "A", "outcome_ok": True, "model_amounts": []},
    ]
    assert report.summarize(rows, trials=2)["g4_median_amounts"] == 1


# --- review round 3: exact rules instead of judgements ----------------------------------


def test_r3_keys_drop_only_the_description_and_keep_sql_exact():
    k = lambda sql, **extra: tape.tape_key(EXT + "ns_runCustomSuiteQL", {"sqlQuery": sql, **extra})  # noqa: E731
    assert k("SELECT id FROM t", description="a") == k("SELECT id FROM t", description="b")
    assert k("SELECT id FROM t -- f\nWHERE id = 1") != k("SELECT id FROM t -- f WHERE id = 1")
    assert k("SELECT  id FROM t") != k("SELECT id FROM t")


@pytest.mark.parametrize(
    "name, tool_input, kind",
    [
        ("bigquery_sql", {"query": "SELECT 1; DELETE FROM d.orders WHERE TRUE"}, "refused"),
        ("cross_source_query", {"query": "SELECT 1"}, "refused"),
        ("pivot_query_result", {"query": "SELECT a FROM `p.d.t`", "dialect": "bigquery"}, "refused"),
        ("pivot_query_result", {"query": "SELECT a FROM transaction"}, "read"),
        ("celigo_flows", {}, "refused"),
    ],
)
def test_r3_only_sources_whose_engine_forbids_writes_are_readable(name, tool_input, kind):
    assert tape.classify(name, tool_input) == kind


@pytest.mark.parametrize(
    "body, taped",
    [
        ({"error": "Invalid search query: unknown identifier 'x'"}, True),  # the agent's own mistake: replays the same
        ({"success": True, "rows": []}, True),
        ({"success": True, "blockers": ["source_refresh:source_rate_limited"]}, False),
        ({"error": "Invalid search query", "blockers": ["source_refresh:source_rate_limited"]}, False),
        ({"error": "Accounting case or scoped configuration unavailable.", "reason": "source_transport_failed"}, False),
        ({"error": "something new"}, False),
    ],
)
async def test_r3_only_clean_results_and_deterministic_query_errors_are_taped(tmp_path, body, taped):
    async def live(tool_name, tool_input, **kwargs):
        return json.dumps(body)

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await d("netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t")
    assert (len(d.tape.entries) == 1) is taped and d.environment_errors == (0 if taped else 1)


@pytest.mark.parametrize(
    "name",
    [
        "transaction_ops_accounting_evidence",
        "transaction_ops_accounting_group",
        "transaction_ops_propose_credit_reallocation",
    ],
)
async def test_r3_known_stateful_reads_are_unreplayable_even_when_state_looked_unchanged(tmp_path, name):
    async def live(tool_name, tool_input, **kwargs):
        return '{"ok": 1}'  # touched nothing visible this time

    recorder = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await recorder(name, {"case_id": "c"}, tenant_id="t", db=_DB())
    player = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    await player(name, {"case_id": "c"}, tenant_id="t", db=_DB())
    assert player.unreplayable == 1


async def test_r3_a_stateful_mark_is_never_cleared(tmp_path):
    db = _DB()

    async def live(tool_name, tool_input, **kwargs):
        kwargs["db"].info["accounting_group_selection"] = {"group_id": "g"}  # the same value every time
        return '{"ok": 1}'

    recorder = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    for _ in range(3):
        await recorder("netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t", db=db)
    player = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    await player("netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t", db=_DB())
    assert player.unreplayable == 1


@pytest.mark.parametrize("review", [{"invoice_id": "77"}, {}])
def test_r3_an_untypable_applied_document_is_unknown(review):
    card = {
        **SALES_CREDIT_CARD,
        "accounting_review": review,
        "proposed_fields": {
            **SALES_CREDIT_CARD["proposed_fields"],
            "apply": {"items": [{"doc": {"id": "99"}, "apply": True}]},
        },
    }
    p = graders.proposal_from_card(card)
    assert p.created_from == "unknown"
    assert "created_from" in graders.grade(_task(_gold(created_from="None")), _attempt(proposals=[p])).payload_diff


@pytest.mark.parametrize(
    "terminal", [{"invariant_errors": ["posting_period_closed"]}, {"unfillable_line_fields": ["item"]}]
)
def test_r3_a_card_production_refuses_to_approve_cannot_pass(terminal):
    p = graders.proposal_from_card({**SALES_CREDIT_CARD, **terminal})
    g = graders.grade(_task(_gold(item="1471 → 40050")), _attempt(proposals=[p]))
    assert p.approvable is False and not g.action_ok and not g.outcome_ok


async def test_r3_a_redirect_cannot_carry_a_replay_off_the_model_host(tmp_path):
    import httpx

    hits = []

    def handler(request):
        hits.append(request.url.host)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(302, headers={"location": "https://solidus.example.com/api/orders"})
        return httpx.Response(200, json={"live": True})

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d):
        async with httpx.AsyncClient(
            transport=tape.guarded_transport(httpx.MockTransport(handler), d), follow_redirects=True
        ) as client:
            with pytest.raises(tape.LiveNetworkBlockedError):
                await client.get("https://api.anthropic.com/v1/x")
    assert hits == ["api.anthropic.com"] and d.network_blocked == 1


def test_r3_the_default_transports_are_guarded_during_replay(tmp_path):
    import httpx

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d):
        assert getattr(httpx.AsyncHTTPTransport.handle_async_request, "_bench_guard", False)
        assert getattr(httpx.HTTPTransport.handle_request, "_bench_guard", False)
    assert not getattr(httpx.AsyncHTTPTransport.handle_async_request, "_bench_guard", False)


# --- review round 4 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, taped",
    [
        ({"success": True, "accounting_evidence": {"blockers": ["source_refresh:source_rate_limited"]}}, False),
        ({"success": True, "accounting_evidence": {"blockers": []}}, True),
    ],
)
async def test_r4_nested_blockers_are_never_taped(tmp_path, body, taped):
    async def live(tool_name, tool_input, **kwargs):
        return json.dumps(body)

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await d("netsuite_suiteql", {"query": "SELECT 1"}, tenant_id="t")
    assert (len(d.tape.entries) == 1) is taped


def test_r4_one_untypable_applied_document_makes_the_origin_unknown():
    fields = {
        **SALES_CREDIT_CARD["proposed_fields"],
        "apply": {"items": [{"doc": {"id": "77"}, "apply": True}, {"doc": {"id": "99"}, "apply": True}]},
    }
    p = graders.proposal_from_card({**SALES_CREDIT_CARD, "proposed_fields": fields})
    assert p.created_from == "unknown"


async def test_r4_setup_model_calls_are_charged(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.benchmarks import agent_runner
    from app.services.benchmarks.resolve import ours

    setup = _anthropic([("json", _message({"input_tokens": 100, "output_tokens": 10}))])
    turn = _anthropic([("json", _message({"input_tokens": 2}))])

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def run_streaming(self, **kwargs):
            await model_call(turn)
            yield "response", AgentResult(success=True, data="ok")

    async def _none(*args, **kwargs):
        return None

    async def _context(**kwargs):
        await model_call(setup)
        return {}

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(agent_runner, "_build_adapter", lambda **kwargs: object())
    monkeypatch.setattr(agent_runner, "get_active_metadata", _none)
    monkeypatch.setattr(agent_runner, "_load_tenant_config", _none)
    monkeypatch.setattr(agent_runner, "_assemble_context", _context)
    monkeypatch.setattr(agent_runner, "UnifiedAgent", FakeAgent)
    monkeypatch.setattr(agent_runner, "_asks_for_source", lambda result: False)

    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="replay", model="m"
    )
    assert (a.input_tokens, a.output_tokens) == (102, 10)


async def test_r4_a_failed_save_keeps_the_previous_results(tmp_path, monkeypatch):
    out = tmp_path / "run.json"
    bench = [tasks.Task(ref=f"R10000000{i}", case_id="c", prompt="p", gold=_gold()) for i in range(2)]
    real_replace, calls = report.os.replace, []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    async def agent(task, trial):
        return _attempt()

    monkeypatch.setattr(report.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        await report.run(bench, agent, trials=1, out_path=out)
    assert len(json.loads(out.read_text())["trials"]) == 1


# --- review round 5: one mechanism per shape ----------------------------------------------------


def _trial_patches(monkeypatch, agent_cls, context):
    from app.core.config import settings
    from app.services.benchmarks import agent_runner

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(agent_runner, "_build_adapter", lambda **kwargs: object())
    monkeypatch.setattr(agent_runner, "get_active_metadata", _none)
    monkeypatch.setattr(agent_runner, "_load_tenant_config", _none)
    monkeypatch.setattr(agent_runner, "_assemble_context", context)
    monkeypatch.setattr(agent_runner, "UnifiedAgent", agent_cls)
    monkeypatch.setattr(agent_runner, "_asks_for_source", lambda result: False)


async def test_r5_every_model_call_in_a_trial_is_charged_wherever_it_is_made(tmp_path, monkeypatch):
    """F21 then F26 were model calls missed by summing usage at known call sites (entity
    resolution, then the confidence check). Tokens are counted at the SDK, which every call
    passes: setup, the agent's turns, a stream, and a side call on another client."""
    from app.services.benchmarks.resolve import ours

    setup_client = _anthropic([("json", _message({"input_tokens": 100, "output_tokens": 10}))])
    agent_client = _anthropic(
        [
            ("json", _message({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7})),
            ("stream", _stream_body(4, 2)),
        ]
    )
    side_client = _anthropic([("json", _message({"input_tokens": 3, "output_tokens": 1}))])

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run_streaming(self, **kwargs):
            await agent_client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}])
            async with agent_client.messages.stream(
                model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]
            ) as s:
                async for _ in s:
                    pass
            await side_client.messages.create(model="h", max_tokens=1, messages=[{"role": "user", "content": "x"}])
            yield "response", AgentResult(success=True, data="ok", tokens_used=TokenUsage(input_tokens=999))

    async def context(**kwargs):
        await setup_client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}])
        return {}

    _trial_patches(monkeypatch, Agent, context)
    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="replay", model="m"
    )
    assert (a.input_tokens, a.output_tokens, a.cache_tokens) == (117, 18, 7)
    assert a.unmetered_model_calls == 0 and a.error is None


async def test_r5_a_model_call_whose_usage_cannot_be_read_makes_the_run_not_comparable(tmp_path, monkeypatch):
    from app.services.benchmarks.resolve import ours

    client = _anthropic([("stream", _stream_body(4, 2))])

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run_streaming(self, **kwargs):
            raw = await client.messages.create(
                model="m", max_tokens=1, stream=True, messages=[{"role": "user", "content": "x"}]
            )
            async for _ in raw:
                pass
            yield "response", AgentResult(success=True, data="ok")

    async def context(**kwargs):
        return {}

    _trial_patches(monkeypatch, Agent, context)
    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="replay", model="m"
    )
    assert a.unmetered_model_calls == 1
    assert graders.grade(tasks.Task(ref="R1", case_id="c", prompt="p", gold=_gold()), a).environment_complete is False


async def test_r5_setup_runs_inside_the_network_guard(tmp_path, monkeypatch):
    """F25: context assembly ran before the guard, so its retrieval could reach other hosts."""
    import httpx

    from app.services.benchmarks.resolve import ours

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run_streaming(self, **kwargs):
            yield "response", AgentResult(success=True, data="ok")

    async def context(**kwargs):
        try:  # retrieval swallows its own failures, as the real helpers do
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
                await c.post("https://api.openai.com/v1/embeddings", json={})
        except Exception:
            pass
        return {}

    _trial_patches(monkeypatch, Agent, context)
    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="replay", model="m"
    )
    assert a.network_blocked == 1


async def test_r5_an_interpreter_failure_makes_the_run_not_comparable(tmp_path):
    """F23: a failed interpretation lowered the score while the run still read comparable."""
    bench = [tasks.Task(ref="R100000001", case_id="c", prompt="p", gold=_gold())]

    async def agent(task, trial):
        return _attempt(resolution=None)  # undeclared, so the interpreter must read it

    async def interpret(text):
        raise RuntimeError("provider down")

    summary = await report.run(bench, agent, trials=1, out_path=tmp_path / "r.json", interpret=interpret)
    assert summary["environment_incomplete_trials"] == 1 and summary["comparable"] is False


async def test_r5_the_interpreter_reads_the_whole_reply_or_refuses(monkeypatch):
    """F24: text after 8,000 characters was dropped, so a final conclusion could be ignored."""
    import anthropic

    from app.services.benchmarks.resolve import interpret as mod

    sent = []

    class Client:
        def __init__(self, **kwargs):
            self.messages = self

        async def create(self, **kwargs):
            sent.append(kwargs["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(text='{"diagnosis": null, "action": null}')])

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    read = mod.make_interpreter(api_key="x", model="m")
    await read("a" * 9000 + " FINAL CONCLUSION")
    assert sent[-1].endswith("FINAL CONCLUSION")
    with pytest.raises(ValueError, match="too long"):
        await read("a" * (mod.MAX_INTERPRET_CHARS + 1))


@pytest.mark.parametrize(("applied", "expected"), [(["77", "99"], "unknown"), (["77"], "Invoice"), ([], "Invoice")])
def test_r5_created_from_never_masks_an_untypable_application(applied, expected):
    """F5: an explicit createdFrom returned before the applied documents were checked."""
    fields = {
        **SALES_CREDIT_CARD["proposed_fields"],
        "createdFrom": {"id": "77"},
        "apply": {"items": [{"doc": {"id": d}, "apply": True} for d in applied]},
    }
    card = {**SALES_CREDIT_CARD, "proposed_fields": fields}
    card["accounting_review"] = {**(card.get("accounting_review") or {}), "invoice_id": "77"}
    assert graders.proposal_from_card(card).created_from == expected


# --- review round 6: the mechanisms' own holes ---------------------------------------------------


def _failing_anthropic():
    import anthropic
    import httpx

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    return anthropic.AsyncAnthropic(
        api_key="x", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _openai(usage):
    import httpx
    import openai

    body = {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "model": "text-embedding-3-small",
        "usage": usage,
    }
    return openai.AsyncOpenAI(
        api_key="x",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))),
    )


def _truncated_stream_body():
    # message_start and the text, then the connection ends: no message_delta, no message_stop.
    full = _stream_body(10, 7)
    return full[: full.index("event: message_delta")]


async def _metered(coro_factory):
    from app.services.benchmarks.resolve.meter import ModelMeter, metered

    meter = ModelMeter()
    with metered(meter):
        try:
            await coro_factory()
        except Exception:
            pass
    return meter


async def test_r6_f27_a_model_call_that_fails_is_unmetered_not_free():
    client = _failing_anthropic()
    meter = await _metered(lambda: model_call(client))
    assert (meter.calls, meter.unmetered) == (1, 1)


async def test_r6_f28_a_stream_that_ends_before_its_final_usage_is_unmetered():
    client = _anthropic([("stream", _truncated_stream_body())])

    async def read():
        async with client.messages.stream(model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]) as s:
            async for _ in s:
                pass

    meter = await _metered(read)
    assert meter.unmetered == 1
    complete = await _metered(lambda: _read_stream(_anthropic([("stream", _stream_body(4, 2))])))
    assert (complete.unmetered, complete.output_tokens) == (0, 2)


async def _read_stream(client):
    async with client.messages.stream(model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]) as s:
        async for _ in s:
            pass


async def test_r6_f29_embedding_tokens_are_metered_separately():
    client = _openai({"prompt_tokens": 19, "total_tokens": 19})
    meter = await _metered(lambda: client.embeddings.create(model="text-embedding-3-small", input="x"))
    assert (meter.embedding_tokens, meter.input_tokens, meter.unmetered) == (19, 0, 0)


async def test_r6_f29_a_trial_reports_its_embedding_tokens(tmp_path, monkeypatch):
    from app.services.benchmarks.resolve import ours

    embedder = _openai({"prompt_tokens": 19, "total_tokens": 19})

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run_streaming(self, **kwargs):
            yield "response", AgentResult(success=True, data="ok")

    async def context(**kwargs):
        await embedder.embeddings.create(model="text-embedding-3-small", input="x")
        return {}

    _trial_patches(monkeypatch, Agent, context)
    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="record", model="m"
    )  # embeddings run live only in record mode
    assert a.embedding_tokens == 19


def test_r6_f30_a_typed_origin_applied_to_a_typed_invoice_keeps_its_type():
    fields = {
        **SALES_CREDIT_CARD["proposed_fields"],
        "createdFrom": {"id": "88", "refName": "Return Authorization #RA88"},
        "apply": {"items": [{"doc": {"id": "77"}, "apply": True}]},
    }
    card = {**SALES_CREDIT_CARD, "proposed_fields": fields}
    card["accounting_review"] = {**(card.get("accounting_review") or {}), "invoice_id": "77"}
    assert graders.proposal_from_card(card).created_from == "Return authorization"


@pytest.mark.parametrize(
    ("text", "stop", "outcome"),
    [
        ('{"diagnosis": null, "action": null}', "end_turn", None),  # the agent reached no conclusion
        ('{"diagnosis": "credited", "action": "explain_close"}', "end_turn", "read"),
        ('{"diagnosis": "credi', "max_tokens", "raises"),  # cut off: the grader failed
        ("not json", "end_turn", "raises"),
        ('{"diagnosis": "made_up", "action": "explain_close"}', "end_turn", "raises"),
    ],
)
async def test_r6_f31_only_a_clean_null_is_the_agents_failure(monkeypatch, text, stop, outcome):
    import anthropic

    from app.services.benchmarks.resolve import interpret as mod

    class Client:
        def __init__(self, **kwargs):
            self.messages = self

        async def create(self, **kwargs):
            return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(text=text)])

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    read = mod.make_interpreter(api_key="x", model="m")
    if outcome == "raises":
        with pytest.raises(ValueError):
            await read("The agent's reply.")
    elif outcome is None:
        assert await read("The agent's reply.") is None
    else:
        assert await read("The agent's reply.") == {"diagnosis": "credited", "action": "explain_close"}


# --- review round 7 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"diagnosis": "credited"}',
        '{"diagnosis": "made_up", "action": null}',
        '{"diagnosis": null, "action": "explain_close"}',
        '{"diagnosis": "credited", "action": "explain_close", "extra": 1}',
    ],
)
def test_r7_f31_the_interpreter_reading_has_one_strict_shape(raw):
    """F31 (third time): every reading that is not exactly {diagnosis, action}, both null or
    both in the vocabularies, is the grader's failure."""
    from app.services.benchmarks.resolve.interpret import parse_interpretation

    with pytest.raises(ValueError):
        parse_interpretation(raw)


RAG_DOWN = {
    "results": [],
    "count": 0,
    "query": "q",
    "note": "Search temporarily unavailable, proceed without documentation context.",
}


async def test_r7_f32_a_degraded_search_is_an_environment_error_not_a_recording(tmp_path):
    """F32: rag.search swallows its failures into an empty result with a note; recording that
    would replay a failed retrieval as a clean one."""

    async def live(tool_name, tool_input, **kwargs):
        return json.dumps(RAG_DOWN)

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await d("rag_search", {"query": "q"}, tenant_id="t", db=SimpleNamespace(info={}))
    assert d.environment_errors == 1 and d.tape.entries == {}


async def test_r7_f32_a_degraded_search_already_on_a_tape_is_not_replayed_as_clean(tmp_path):
    t = tape.Tape(tmp_path / "t.jsonl")
    t.put(
        tape.tape_key("rag_search", {"query": "q"}, tenant_id="t"), "rag_search", {"query": "q"}, json.dumps(RAG_DOWN)
    )
    d = tape.TapedDispatcher(t, mode="replay")
    await d("rag_search", {"query": "q"}, tenant_id="t", db=SimpleNamespace(info={}))
    assert d.environment_errors == 1


async def test_r7_f32_an_empty_but_healthy_search_is_still_a_clean_read(tmp_path):
    async def live(tool_name, tool_input, **kwargs):
        return json.dumps({"results": [], "count": 0, "query": "q"})

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await d("rag_search", {"query": "q"}, tenant_id="t", db=SimpleNamespace(info={}))
    assert d.environment_errors == 0 and len(d.tape.entries) == 1


# --- review round 8: reads that swallow failures, observed at the IO layer -----------------------


def _http(handler):
    import httpx

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _swallowing_read(client, url):
    """A read tool that, like several of ours, turns its own failure into a clean-looking result."""
    try:
        await client.get(url)
    except Exception:
        pass
    return json.dumps({"results": [], "count": 0})


@pytest.mark.parametrize("failure", ["raises", "503"])
async def test_r8_a_read_whose_io_failed_is_an_environment_error_and_never_recorded(tmp_path, failure):
    import httpx

    def handler(request):
        if failure == "raises":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(503)

    client = _http(handler)

    async def live(tool_name, tool_input, **kwargs):
        return await _swallowing_read(client, "https://search.example.com/q")

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    with tape.installed(d):
        await d("transaction_ops_status", {"case_id": "c"}, tenant_id="t", db=SimpleNamespace(info={}))
    assert (d.io_failures, d.environment_errors, d.tape.entries) == (1, 1, {})


def test_r8_a_database_error_is_an_io_failure_but_an_integrity_error_is_not(tmp_path):
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        c.execute(sa.text("INSERT INTO t VALUES (1)"))
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d):
        for sql in ("SELECT * FROM missing_table", "INSERT INTO t VALUES (1)"):
            try:
                with engine.begin() as c:
                    c.execute(sa.text(sql))
            except Exception:
                pass
    assert d.io_failures == 1  # the missing table, not the expected duplicate key


async def test_r8_a_retried_model_call_is_the_meters_business_not_an_io_failure(tmp_path):
    import anthropic
    import httpx

    answers = [
        httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}),
        httpx.Response(200, json=_message({"input_tokens": 1})),
    ]
    client = anthropic.AsyncAnthropic(api_key="x", max_retries=1, http_client=_http(lambda request: answers.pop(0)))
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d):
        await model_call(client)
    assert d.io_failures == 0


async def test_r8_a_trial_whose_setup_swallowed_an_io_failure_is_not_comparable(tmp_path, monkeypatch):
    import httpx

    from app.services.benchmarks.resolve import ours

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run_streaming(self, **kwargs):
            yield "response", AgentResult(success=True, data="ok")

    async def context(**kwargs):
        await _swallowing_read(_http(handler), "https://retrieval.example.com/q")
        return {}

    _trial_patches(monkeypatch, Agent, context)
    task = tasks.Task(ref="R100000000", case_id="c", prompt="p", gold=None)
    a = await ours.run_ours(
        task, 0, db=None, tenant_id="t", actor_id="u", tape=tape.Tape(tmp_path / "t.jsonl"), mode="record", model="m"
    )
    assert a.io_failures == 1
    assert graders.grade(tasks.Task(ref="R1", case_id="c", prompt="p", gold=_gold()), a).environment_complete is False


async def test_r8_record_mode_never_reuses_a_degraded_entry_as_clean(tmp_path):
    t = tape.Tape(tmp_path / "t.jsonl")
    t.put(
        tape.tape_key("rag_search", {"query": "q"}, tenant_id="t"), "rag_search", {"query": "q"}, json.dumps(RAG_DOWN)
    )

    async def live(tool_name, tool_input, **kwargs):
        raise AssertionError("a cache hit must not call live")

    d = tape.TapedDispatcher(t, mode="record", live=live)
    await d("rag_search", {"query": "q"}, tenant_id="t", db=SimpleNamespace(info={}))
    assert d.environment_errors == 1


@pytest.mark.parametrize(
    ("netsuite", "saved", "degraded"),
    [
        ("timed_out", "complete", True),
        ("complete", "unavailable", True),
        ("unavailable", "complete", True),
        ("complete", "complete", False),
        ("not_needed", "complete", False),
    ],
)
async def test_r8_a_group_breakdown_that_could_not_check_is_degraded(tmp_path, netsuite, saved, degraded):
    async def live(tool_name, tool_input, **kwargs):
        return json.dumps({"success": True, "checked": {"netsuite": netsuite, "saved_source": saved}, "causes": []})

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await d("transaction_ops_group_breakdown", {"group_id": "g"}, tenant_id="t", db=SimpleNamespace(info={}))
    assert d.environment_errors == (1 if degraded else 0)


def test_r8_a_tape_recorded_before_failure_detection_is_refused(tmp_path):
    old = tmp_path / "old.jsonl"
    old.write_text(json.dumps({"key": "k", "result": "{}", "state_keys": []}) + "\n")
    with pytest.raises(ValueError, match="re-record"):
        tape.Tape(old)
    new = tape.Tape(tmp_path / "new.jsonl")
    new.put("k", "rag_search", {"query": "q"}, "{}")
    assert tape.Tape(tmp_path / "new.jsonl").get("k")["result"] == "{}"


# --- review round 9: only tools with observable IO; the trial's own result cache ---------------


def test_r9_a_tool_whose_io_the_harness_cannot_observe_is_not_offered():
    """F36-F38: accounting_reference reads through DDGS/primp and streamed bodies and turns
    every failure into a normal result; resolving a case does not need it."""
    assert tape.classify("transaction_ops_accounting_reference") == "refused"


async def test_r9_f36_a_streamed_body_that_fails_after_send_is_an_io_failure(tmp_path):
    import httpx

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("connection reset")

    client = _http(lambda request: httpx.Response(200, stream=Broken()))
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d, allow_hosts=frozenset({"docs.example.com"})):
        try:
            async with client.stream("GET", "https://docs.example.com/page") as response:
                async for _ in response.aiter_bytes():
                    pass
        except httpx.HTTPError:
            pass
    assert d.io_failures == 1


async def test_r9_f37_an_aiohttp_failure_is_an_io_failure(tmp_path, monkeypatch):
    import aiohttp

    async def broken(self, method, url, **kwargs):
        raise aiohttp.ClientConnectionError("down")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", broken)
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=lambda *a, **k: None)
    with tape.installed(d):
        async with aiohttp.ClientSession() as session:
            try:
                await session._request("POST", "https://api.voyageai.com/v1/embeddings")
            except aiohttp.ClientError:
                pass
    assert d.io_failures == 1


@pytest.mark.parametrize(
    ("status", "counted"), [(401, True), (403, True), (407, True), (400, False), (404, False), (422, False)]
)
async def test_r9_f38_auth_failures_are_environment_failures_but_query_mistakes_are_not(tmp_path, status, counted):
    import httpx

    client = _http(lambda request: httpx.Response(status))
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=lambda *a, **k: None)
    with tape.installed(d):
        await client.get("https://netsuite.example.com/record")
    assert d.io_failures == (1 if counted else 0)


async def test_r9_f39_each_trial_has_its_own_result_cache_and_never_reaches_redis(tmp_path, monkeypatch):
    import redis

    from app.services.chat import result_cache

    def no_redis(*args, **kwargs):
        raise AssertionError("a benchmark trial reached Redis")

    monkeypatch.setattr(redis, "from_url", no_redis)
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    with tape.installed(d):
        result_cache.cache_full_payload("conv", "r1", {"rows": [1]})
        entry = result_cache.get_full_payload_entry("conv", "r1")
    assert entry is not None  # the round trip ran in process, without Redis
    with tape.installed(tape.TapedDispatcher(tape.Tape(tmp_path / "u.jsonl"), mode="replay")):
        assert result_cache.get_full_payload_entry("conv", "r1") is None  # nothing leaks between trials
    assert result_cache._get_redis.__name__ == "_get_redis"  # restored after the trial
