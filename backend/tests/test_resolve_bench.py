"""Resolve benchmark harness (spec 2026-10-01 §7, block B3): tasks, the read tape, graders, report.

Synthetic order references only: real Framework cases and labels never enter git.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from app.services.benchmarks.resolve import graders, report, tape, tasks
from app.services.benchmarks.resolve.graders import Attempt, Proposal
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


def test_the_key_ignores_free_text_descriptions_and_sql_layout():
    a = tape.tape_key(EXT + "ns_runCustomSuiteQL", {"sqlQuery": "SELECT id\nFROM transaction", "description": "x"})
    b = tape.tape_key(EXT + "ns_runCustomSuiteQL", {"sqlQuery": "SELECT  id FROM transaction"})
    c = tape.tape_key(EXT + "ns_runCustomSuiteQL", {"sqlQuery": "SELECT tranid FROM transaction"})
    assert a == b != c


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


async def test_replay_blocks_netsuite_token_refresh(tmp_path):
    from app.services import netsuite_oauth_service as oauth

    original = oauth.refresh_tokens
    with tape.installed(tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")):
        with pytest.raises(tape.LiveNetSuiteBlockedError):
            await oauth.refresh_tokens("acct", "token")
        with pytest.raises(tape.LiveNetSuiteBlockedError):
            await oauth.refresh_tokens_with_client("acct", "token", "client")
    assert oauth.refresh_tokens is original


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
    a = attempt_from_run(events, d, wall_ms=1234)
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
    assert (a.reply_text, a.input_tokens, a.tool_calls) == ("Done.", 10, 1)


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

    class FakeAgent:
        turns = 0

        def __init__(self, **kwargs):
            seen_actors.append(kwargs["user_id"])

        async def run_streaming(self, *, task, context, db, adapter, model, conversation_history):
            FakeAgent.turns += 1
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


def test_f8_whitespace_inside_sql_literals_is_kept():
    k = lambda sql: tape.tape_key("netsuite_suiteql", {"query": sql})  # noqa: E731
    assert k("SELECT id FROM customer WHERE companyname = 'A  B'") != k(
        "SELECT id FROM customer WHERE companyname = 'A B'"
    )
    assert k("SELECT id\n  FROM customer WHERE x = 'it''s  ok'") == k("SELECT id FROM customer WHERE x = 'it''s  ok'")


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


async def test_f3_session_state_a_read_leaves_is_restored_on_replay(tmp_path):
    import uuid as _uuid

    candidate = {"amount": Decimal("4.82"), "case": _uuid.UUID(int=7), "lines": [{"rate": Decimal("1.5")}]}

    async def live(tool_name, tool_input, **kwargs):
        kwargs["db"].info["accounting_correction_candidate"] = candidate
        kwargs["db"].info.pop("stale", None)
        return '{"ok": 1}'

    recording_db = _DB()
    recording_db.info["stale"] = 1
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    await d("transaction_ops_accounting_evidence", {"case_id": "c"}, tenant_id="t", db=recording_db)

    replay_db = _DB()
    replay_db.info["stale"] = 1
    player = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    assert (
        await player("transaction_ops_accounting_evidence", {"case_id": "c"}, tenant_id="t", db=replay_db)
        == '{"ok": 1}'
    )
    assert replay_db.info == {"accounting_correction_candidate": candidate}


async def test_f3_state_that_cannot_be_recorded_fails_loudly(tmp_path):
    async def live(tool_name, tool_input, **kwargs):
        kwargs["db"].info["accounting_correction_candidate"] = object()
        return "{}"

    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="record", live=live)
    with pytest.raises(tape.TapeStateError):
        await d("transaction_ops_accounting_evidence", {}, tenant_id="t", db=_DB())


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
        ('{"diagnosis": "made_up", "action": "explain_close"}', None),
        ("not json", None),
    ],
)
def test_f1_the_interpreter_accepts_only_the_label_vocabularies(raw, expected):
    from app.services.benchmarks.resolve.interpret import parse_interpretation

    assert parse_interpretation(raw) == expected


def test_f12_summary_counts_trials_with_amounts():
    rows = [
        {"ref": "A", "outcome_ok": True, "model_amounts": ["$1.00", "2.00"]},
        {"ref": "A", "outcome_ok": True, "model_amounts": []},
    ]
    s = report.summarize(rows, trials=2)
    assert (s["g4_model_amounts"], s["g4_trials_with_amounts"]) == (2, 1)
