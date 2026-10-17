"""The scorecard: our agent against native Claude + MCP on the same cases (Aiden, 2026-10-10:
"evaluate and metric and output the result")."""

import json
from types import SimpleNamespace

from app.services.benchmarks.resolve import scorecard


def _row(ref, ok, reason, *, graded=True, words=40, tokens=1000, wall=9000, trial=0):
    return {
        "ref": ref,
        "trial": trial,
        "outcome_ok": ok,
        "outcome_reason": reason,
        "graded": graded,
        "words": words,
        "tokens": tokens,
        "wall_ms": wall,
        "model_amounts": [],
        "environment_complete": True,
    }


def _results(rows, agent):
    return {"meta": {"agent": agent}, "summary": {"comparable": True}, "trials": rows}


def test_the_scorecard_compares_both_agents_on_the_cases_both_could_be_graded_on():
    ours = _results(
        [
            _row("R1", True, "order_equals_source", words=30, tokens=900, wall=4000),
            _row("R2", False, "outcome_does_not_match_source"),
            _row("R3", None, "netsuite_below_source", graded=False),
        ],
        "ours",
    )
    native = _results(
        [
            _row("R1", True, "order_equals_source", words=120, tokens=5000, wall=30000),
            _row("R2", True, "order_equals_source", words=150, tokens=6000, wall=40000),
            _row("R3", None, "netsuite_below_source", graded=False),
        ],
        "reference",
    )
    card = scorecard.compare(ours, native)
    assert card["cases"] == {"graded_by_both": 2, "ungraded": {"netsuite_below_source": 1}}
    assert (card["ours"]["g1_pass_at_1"], card["native"]["g1_pass_at_1"]) == (0.5, 1.0)
    assert card["goals"]["G2_not_worse_than_native"] is False
    assert (card["ours"]["median_words"], card["native"]["median_words"]) == (35.0, 135.0)
    assert card["ours"]["median_tokens_resolved"] == 900 and card["native"]["median_tokens_resolved"] == 5500.0
    assert [c["ref"] for c in card["per_case"]] == ["R1", "R2"]
    assert card["per_case"][1] == {
        "ref": "R2",
        "ours": "outcome_does_not_match_source",
        "native": "pass",
    }


def test_the_scorecard_says_when_a_run_is_not_comparable():
    ours = _results([_row("R1", True, "order_equals_source")], "ours")
    native = {**_results([_row("R1", True, "order_equals_source")], "reference"), "summary": {"comparable": False}}
    assert scorecard.compare(ours, native)["comparable"] is False


def test_the_scorecard_renders_as_a_readable_table():
    ours = _results([_row("R1", True, "order_equals_source")], "ours")
    native = _results([_row("R1", False, "expected_a_credit")], "reference")
    text = scorecard.render_markdown(scorecard.compare(ours, native))
    assert "| G1 right fix (pass@1) |" in text and "| R1 | pass | expected_a_credit |" in text


async def test_tasks_are_the_open_cases_of_the_tenant_in_one_subsidiary(tmp_path, monkeypatch):
    from app.services.benchmarks.resolve import __main__ as cli

    cases = [
        SimpleNamespace(id=f"c{i}", order_reference=f"R{i}", scope_json={"subsidiary_id": "1" if i % 2 else "2"})
        for i in range(5)
    ]

    async def list_cases(db, tenant_id, *, status=None, limit=100, offset=0):
        assert status == "open"
        return cases[offset : offset + limit]

    class Session:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.services.transaction_ops.case_service.list_cases", list_cases)
    out = tmp_path / "tasks.json"
    count = await cli._build_tasks(
        out, "00000000-0000-0000-0000-000000000000", subsidiary="1", session_factory=Session, page=2
    )
    assert count == 2 and json.loads(out.read_text()) == [
        {"ref": "R1", "case_id": "c1"},
        {"ref": "R3", "case_id": "c3"},
    ]
