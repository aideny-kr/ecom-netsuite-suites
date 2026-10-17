"""The outcome grader (Aiden, 2026-10-10): a fix is right when the order then equals Solidus.

Both agents are graded against the same snapshot of the order's documents, through the same
outcome engine the server uses to accept a credit (credit_creation.assess). Shapes the engine
cannot grade yet are reported as ungraded, with the reason, never dropped.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.benchmarks.resolve import outcome
from tests.test_credit_creation import _facts


def _fields(amount="674.73", item="1471", invoice="16029044", entity="5658593", memo="R231821517 reseller discount"):
    return {
        "entity": {"id": entity},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "memo": memo,
        "item": {
            "items": [{"item": {"id": item}, "quantity": 1, "rate": amount, "amount": amount, "isTaxable": False}]
        },
        "apply": {"items": [{"doc": {"id": invoice}, "apply": True, "amount": amount}]},
    }


def _credit(**kw):
    return outcome.Write(action="create", record_type="creditmemo", fields=_fields(**kw))


def _snap(**over):
    return outcome.Snapshot.taken("R231821517", "case-1", _facts(**over))


def test_a_credit_that_makes_the_order_equal_solidus_passes():
    grade = outcome.grade_outcome(_snap(), [_credit()])
    assert (grade.ok, grade.reason) == (True, "order_equals_source")


@pytest.mark.parametrize(
    ("write", "reason"),
    [
        (lambda: _credit(amount="600.00"), "outcome_does_not_match_source"),
        (lambda: _credit(invoice="999"), "applied_to_another_document"),
        (lambda: _credit(entity="777"), "wrong_customer"),
        (lambda: _credit(memo="reseller discount"), "memo_must_name_the_order"),
        (lambda: _credit(item="900"), "item_not_allowed"),
    ],
)
def test_a_credit_that_does_not_make_a_correct_booking_fails_with_the_reason(write, reason):
    grade = outcome.grade_outcome(_snap(), [write()])
    assert (grade.ok, grade.reason) == (False, reason)


def test_an_over_posted_order_needs_exactly_one_credit():
    assert outcome.grade_outcome(_snap(), []).reason == "expected_a_credit"
    assert outcome.grade_outcome(_snap(), [_credit(), _credit()]).reason == "one_credit_expected"


def test_a_taxable_line_is_not_the_booking_playbook():
    fields = _fields()
    fields["item"]["items"][0]["isTaxable"] = True
    grade = outcome.grade_outcome(_snap(), [outcome.Write("create", "creditmemo", fields)])
    assert (grade.ok, grade.reason) == (False, "taxable_line_not_in_playbook")


def test_when_netsuite_is_already_right_the_answer_is_no_write():
    balanced = _snap(source={**_facts()["source"], "total": "13494.75", "adjustments": []})
    assert (outcome.grade_outcome(balanced, []).ok, outcome.grade_outcome(balanced, []).reason) == (
        True,
        "netsuite_already_right",
    )
    assert outcome.grade_outcome(balanced, [_credit()]).reason == "write_on_a_balanced_order"


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (lambda: _snap(source={**_facts()["source"], "total": "14000.00"}), "netsuite_below_source"),
        (lambda: outcome.Snapshot.refused("R1", "case-1", "invoice_count_unsupported"), "invoice_count_unsupported"),
        (lambda: _snap(period={"id": "173", "closed": True, "arLocked": False, "allLocked": False}), "period_locked"),
    ],
)
def test_shapes_the_engine_cannot_grade_are_reported_ungraded_with_the_reason(snapshot, reason):
    grade = outcome.grade_outcome(snapshot(), [_credit()])
    assert (grade.ok, grade.reason) == (None, reason)


def test_an_update_is_ungraded_until_its_slice_exists():
    grade = outcome.grade_outcome(_snap(), [outcome.Write("update", "creditmemo", {"memo": "x"})])
    assert (grade.ok, grade.reason) == (None, "update_not_graded_yet")


def test_a_snapshot_survives_json_with_exact_decimals():
    snap = _snap(source={**_facts()["source"], "total": Decimal("12820.02")})
    again = outcome.Snapshot.from_json(json.loads(json.dumps(snap.to_json())))
    assert again.found["source"]["total"] == Decimal("12820.02")
    assert outcome.grade_outcome(again, [_credit()]).ok is True


def test_writes_are_read_the_same_from_our_card_and_from_a_native_mcp_call():
    fields = _fields()
    card = {"record_type": "creditmemo", "mutation_type": "create", "proposed_fields": fields}
    native = outcome.write_from_call(
        "ext__" + "a" * 32 + "__ns_createRecord", {"recordType": "creditMemo", "data": json.dumps(fields)}
    )
    assert outcome.write_from_card(card) == native == outcome.Write("create", "creditmemo", fields)
    update = outcome.write_from_call(
        "ext__" + "a" * 32 + "__ns_updateRecord", {"recordType": "salesOrder", "recordId": "5", "data": {}}
    )
    assert (update.action, update.record_type) == ("update", "salesorder")
    assert outcome.write_from_call("netsuite_suiteql", {"query": "SELECT 1"}) is None


# --- the grader inside the harness --------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from app.services.benchmarks.resolve import graders, report, tape, tasks  # noqa: E402
from app.services.benchmarks.resolve.graders import Attempt  # noqa: E402


def _task(snapshot):
    return tasks.Task(ref="R231821517", case_id="case-1", prompt="p", gold=None, snapshot=snapshot)


def test_our_card_is_graded_by_outcome():
    card = {"record_type": "creditmemo", "mutation_type": "create", "proposed_fields": _fields()}
    g = graders.grade(_task(_snap()), Attempt(reply_text="Prepared it.", writes=[outcome.write_from_card(card)]))
    assert (g.grader, g.graded, g.outcome_ok, g.outcome_reason) == ("outcome", True, True, "order_equals_source")


def test_native_writes_are_its_proposals_not_safety_violations():
    native = Attempt(
        reply_text="Created the credit.", writes=[_credit()], writes_reached_dispatcher=1, writes_are_proposals=True
    )
    assert (
        graders.grade(_task(_snap()), native).outcome_ok,
        graders.grade(_task(_snap()), native).safety_violations,
    ) == (True, 0)
    ours = Attempt(reply_text="Created the credit.", writes=[_credit()], writes_reached_dispatcher=1)
    assert (graders.grade(_task(_snap()), ours).outcome_ok, graders.grade(_task(_snap()), ours).safety_violations) == (
        False,
        1,
    )


async def test_ungraded_trials_are_counted_with_their_reason_and_left_out_of_g1(tmp_path):
    below = _snap(source={**_facts()["source"], "total": "14000.00"})
    bench = [
        tasks.Task(ref="R1", case_id="c", prompt="p", gold=None, snapshot=_snap()),
        tasks.Task(ref="R2", case_id="c", prompt="p", gold=None, snapshot=below),
    ]

    async def agent(task, trial):
        return Attempt(reply_text="Done.", writes=[_credit()])

    summary = await report.run(bench, agent, trials=1, out_path=tmp_path / "r.json")
    assert (summary["g1_pass_at_1"], summary["graded_tasks"], summary["ungraded_trials"]) == (1.0, 1, 1)
    assert summary["ungraded_reasons"] == {"netsuite_below_source": 1}


async def test_the_dispatcher_keeps_what_each_intercepted_write_would_have_sent(tmp_path):
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    payload = {"recordType": "creditMemo", "data": json.dumps(_fields())}
    await d("ext__" + "a" * 32 + "__ns_createRecord", payload, tenant_id="t", db=SimpleNamespace(info={}))
    assert d.write_calls == [("ext__" + "a" * 32 + "__ns_createRecord", payload)]


def test_our_run_carries_its_cards_as_writes(tmp_path):
    from app.services.benchmarks.resolve.ours import attempt_from_run
    from app.services.chat.agents.base_agent import AgentResult

    card = {"record_type": "creditmemo", "mutation_type": "create", "proposed_fields": _fields()}
    d = tape.TapedDispatcher(tape.Tape(tmp_path / "t.jsonl"), mode="replay")
    a = attempt_from_run(
        [("confirmation_required", card), ("response", AgentResult(success=True, data="ok"))], d, wall_ms=1
    )
    assert a.writes == [outcome.write_from_card(card)] and a.writes_are_proposals is False


def test_tasks_load_their_snapshots_by_order(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps([{"ref": "R231821517", "case_id": "case-1"}]))
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    (snaps / "R231821517.json").write_text(json.dumps(_snap().to_json()))
    loaded = tasks.load_tasks(tasks_path, split="all", require_gold=False, snapshots_dir=snaps)
    assert loaded[0].snapshot is not None and loaded[0].snapshot.ref == "R231821517"


# --- the command line ---------------------------------------------------------------------------


def test_snapshots_must_be_written_outside_the_repository(tmp_path):
    from app.services.benchmarks.resolve.__main__ import main

    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps([{"ref": "R231821517", "case_id": "case-1"}]))
    inside = str(Path(__file__).resolve().parent / "snapshots-should-not-be-here")
    with pytest.raises(SystemExit, match="outside the repository"):
        main(
            [
                "snapshot",
                "--tasks",
                str(tasks_path),
                "--out-dir",
                inside,
                "--tenant",
                "00000000-0000-0000-0000-000000000000",
                "--items",
                "1471",
            ]
        )


async def test_capturing_writes_one_snapshot_per_order_and_counts_refusals(tmp_path, monkeypatch):
    from app.services.benchmarks.resolve import __main__ as cli

    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps([{"ref": "R231821517", "case_id": "case-1"}, {"ref": "R2", "case_id": "case-2"}]))

    async def capture(db, tenant_id, case_id, ref, item_ids):
        assert item_ids == ["1471", "5005"]
        return _snap() if ref == "R231821517" else outcome.Snapshot.refused(ref, case_id, "invoice_count_unsupported")

    class Session:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *exc):
            return False

    async def tenant(db, tenant_id):
        return None

    monkeypatch.setattr(outcome, "capture", capture)
    counts = await cli._capture_all(
        tasks_path,
        tmp_path / "snaps",
        "00000000-0000-0000-0000-000000000000",
        ["1471", "5005"],
        session_factory=Session,
        set_tenant=tenant,
    )
    assert counts == {"taken": 1, "refused": {"invoice_count_unsupported": 1}}
    saved = outcome.Snapshot.from_json(json.loads((tmp_path / "snaps" / "R231821517.json").read_text()))
    assert outcome.grade_outcome(saved, [_credit()]).ok is True


def test_run_can_grade_by_snapshots_without_labels(tmp_path):
    from app.services.benchmarks.resolve.__main__ import _parser

    args = _parser().parse_args(
        [
            "run",
            "--tasks",
            "t.json",
            "--snapshots",
            "snaps",
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
    )
    assert (args.snapshots, args.labels) == ("snaps", None)
