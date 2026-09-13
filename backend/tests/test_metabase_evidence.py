import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.metabase_evidence import UNVERIFIED, MetabaseEvidence
from tests.test_metabase_skills import _agent, _connector

TOOL = "ext__11111111111111111111111111111111__query"
OTHER = "ext__22222222222222222222222222222222__query"


def query(*, grouped=False, aggregate=True, operation="distinct", batch=395, limit=None):
    stage = {
        "lib/type": "mbql.stage/mbql",
        "source-table": ["db", "public", "lines"],
        "filters": [["=", {}, ["field", {}, ["db", "public", "lines", "batch_id"]], batch]],
    }
    if aggregate:
        stage["aggregation"] = [[operation, {}, ["field", {}, ["db", "public", "lines", "order_id"]]]]
    if grouped:
        stage["breakout"] = [["field", {}, ["db", "public", "lines", "state"]]]
    if limit:
        stage["limit"] = limit
    return {"query": {"lib/type": "mbql/query", "stages": [stage]}}


def result(rows, *, grouped=False, aggregate=True, **extra):
    columns = ([{"name": "state", "source": "breakout"}] if grouped else []) + [
        {"name": "orders" if aggregate else "order_id", "source": "aggregation" if aggregate else "fields"}
    ]
    return json.dumps({"status": "completed", "data": {"cols": columns, "rows": rows}, **extra})


def observed(evidence, rows, *, grouped=False, params=None, tool=TOOL, **extra):
    return json.loads(evidence.observe(tool, params or query(grouped=grouped), result(rows, grouped=grouped, **extra)))


def test_manual_sku_count_cannot_be_reported_from_detail_rows():
    evidence = MetabaseEvidence({TOOL})
    output = observed(evidence, [[1001], [1002]], params=query(aggregate=False), aggregate=False)
    assert not output["server_aggregate"]
    assert "mb_ref" not in str(output["rows"])
    assert evidence.feedback("FRANVY0017: 44 orders")
    assert evidence.feedback("FRANVY0017: forty-four orders")
    assert not evidence.feedback("Matching order details:\n" + output["table_reference"])
    assert "1001" in evidence.resolve(output["table_reference"])


def test_headline_and_grouped_counts_render_exact_server_values():
    evidence = MetabaseEvidence({TOOL})
    status = observed(evidence, [["complete", 41], ["canceled", 24]], grouped=True)
    assert evidence.feedback(status["table_reference"])
    total = observed(evidence, [[65]])
    answer = "Matching orders: " + total["rows"][0][0] + "\n\n" + status["table_reference"]
    assert evidence.feedback(answer) is None
    rendered = evidence.resolve(answer)
    assert "Matching orders: 65" in rendered and "| complete | 41 |" in rendered and "| canceled | 24 |" in rendered
    assert "mb_ref" not in rendered


def test_control_must_match_connector_and_population():
    evidence = MetabaseEvidence({TOOL, OTHER})
    grouped = observed(evidence, [["complete", 41]], grouped=True)
    observed(evidence, [[41]], tool=OTHER)
    observed(evidence, [[41]], params=query(batch=396))
    assert evidence.feedback(grouped["table_reference"])
    observed(evidence, [[41]])
    assert evidence.feedback(grouped["table_reference"]) is None


def test_control_query_preserves_population_and_measure_without_group_presentation():
    evidence = MetabaseEvidence({TOOL})
    params = query(grouped=True, limit=50)
    params["query"]["stages"][-1]["joins"] = [{"alias": "orders", "strategy": "inner-join"}]
    grouped = observed(evidence, [["complete", 41]], grouped=True, params=params)
    control = grouped["control_query"]
    assert control["stages"][-1]["joins"] == params["query"]["stages"][-1]["joins"]
    assert "breakout" not in control["stages"][-1] and "limit" not in control["stages"][-1]
    observed(evidence, [[41]], params={"query": control})
    assert evidence.feedback(grouped["table_reference"]) is None


def test_saved_question_uses_server_returned_query_and_malformed_metadata_is_ignored():
    name = TOOL.replace("__query", "__execute_question")
    evidence = MetabaseEvidence({name})
    data = json.loads(result([[65]]))
    data["json_query"] = query()["query"]
    output = json.loads(evidence.observe(name, {"question_id": 123}, json.dumps(data)))
    assert evidence.resolve(output["rows"][0][0]) == "65"
    data["data"]["cols"].append(None)
    raw = json.dumps(data)
    assert evidence.observe(name, {}, raw) == raw


def test_feedback_identifies_scope_literals_without_requiring_a_new_query():
    evidence = MetabaseEvidence({TOOL})
    total = observed(evidence, [[65]])
    feedback = evidence.feedback("Batch 5 (395): " + total["rows"][0][0])
    assert '"5"' in feedback and '"395"' in feedback and "do not need to be requeried" in feedback


def test_grounded_chart_values_render_before_json_is_parsed():
    evidence = MetabaseEvidence({TOOL})
    total = observed(evidence, [[65]])
    answer = '<chart>{"type":"bar","data":[{"label":"Orders","value":' + total["rows"][0][0] + "}]}</chart>"
    assert evidence.feedback(answer) is None
    rendered = evidence.resolve(answer)
    assert json.loads(rendered.removeprefix("<chart>").removesuffix("</chart>"))["data"][0]["value"] == 65


def test_unknown_reference_in_stripped_reasoning_does_not_crash_rendering():
    from app.services.chat.agents.base_agent import strip_confidence_tag

    evidence = MetabaseEvidence({TOOL})
    total = observed(evidence, [[65]])
    answer = "Orders: " + total["rows"][0][0] + "\n<reasoning>{{mb_ref:unknown}}</reasoning>"
    assert evidence.feedback(strip_confidence_tag(answer)) is None
    assert strip_confidence_tag(evidence.resolve(answer)) == "Orders: 65"


def test_distinct_groups_may_overlap_but_cannot_exceed_total():
    evidence = MetabaseEvidence({TOOL})
    grouped = observed(evidence, [["SKU-A", 41], ["SKU-B", 30]], grouped=True)
    observed(evidence, [[65]])
    assert evidence.feedback(grouped["table_reference"]) is None
    impossible = observed(evidence, [["SKU-A", 66]], grouped=True)
    assert evidence.feedback(impossible["table_reference"])


@pytest.mark.parametrize("second, expected", [(24, "sum to"), (30, "overlap")])
def test_reconciliation_narrative_comes_from_actual_control(second, expected):
    evidence = MetabaseEvidence({TOOL})
    grouped = observed(evidence, [["SKU-A", 41], ["SKU-B", second]], grouped=True)
    reference = grouped["control_checks"][0]["control_reference"]
    assert evidence.feedback(reference)
    total = observed(evidence, [[65]])
    assert evidence.feedback(reference) is None
    assert expected in evidence.resolve(reference)
    assert total["control_checks"][0]["result"] == evidence.resolve(reference)


def test_model_cannot_append_contradictory_membership_claim_to_verified_control():
    evidence = MetabaseEvidence({TOOL})
    grouped = observed(evidence, [["SKU-A", 41], ["SKU-B", 24]], grouped=True)
    observed(evidence, [[65]])
    reference = grouped["control_checks"][0]["control_reference"]
    for claim in (
        "Some orders contain multiple matching SKUs.",
        "These counts do not sum to the headline.",
        "There is overlap across SKUs.",
    ):
        assert evidence.feedback(reference + " " + claim)
    answer = grouped["table_reference"] + "\n\n" + reference
    assert evidence.feedback(answer) is None
    assert "sum to the overall distinct count" in evidence.resolve(answer)


def test_additive_counts_must_reconcile_exactly():
    evidence = MetabaseEvidence({TOOL})
    grouped = observed(
        evidence, [["complete", 44], ["canceled", 24]], grouped=True, params=query(grouped=True, operation="count")
    )
    observed(evidence, [[65]], params=query(operation="count"))
    assert "reconcile" in evidence.feedback(grouped["table_reference"])


@pytest.mark.parametrize("extra", [{"continuation_token": "next"}, {"truncated": True}, {"status": "running"}])
def test_partial_aggregate_cannot_authorize_figures(extra):
    evidence = MetabaseEvidence({TOOL})
    data = json.loads(result([[65]]))
    data.update(extra)
    output = json.loads(evidence.observe(TOOL, query(), json.dumps(data)))
    assert not output["complete"]
    assert "mb_ref" not in str(output["rows"])
    assert evidence.feedback(output["table_reference"])


def test_group_limit_and_unknown_or_previous_turn_references_fail_closed():
    evidence = MetabaseEvidence({TOOL})
    output = observed(evidence, [["complete", 41]], grouped=True, params=query(grouped=True, limit=1))
    assert not output["complete"]
    assert evidence.feedback(output["table_reference"])
    other_turn = MetabaseEvidence({TOOL})
    assert other_turn.feedback(output["table_reference"])
    assert evidence.feedback("{{mb_ref:invented:44}}")


@pytest.mark.parametrize("streaming", [True, False])
async def test_agent_withholds_fabricated_figures_then_resolves_aggregate_evidence(streaming):
    connector = _connector(names=["query"])
    agent = _agent([connector])
    name = f"ext__{connector.id.hex}__query"
    agent._metabase_evidence = MetabaseEvidence({name})
    agent._context_need = "data"
    calls = []

    async def response(**kwargs):
        calls.append(deepcopy(kwargs["messages"]))
        step = len(calls)
        if step == 1:
            return LLMResponse(
                text_blocks=[],
                tool_use_blocks=[ToolUseBlock(id="details", name=name, input=query(aggregate=False))],
                usage=TokenUsage(),
            )
        if step == 2:
            return LLMResponse(text_blocks=["There are 44 orders."], tool_use_blocks=[], usage=TokenUsage())
        if step == 3:
            return LLMResponse(
                text_blocks=[], tool_use_blocks=[ToolUseBlock(id="total", name=name, input=query())], usage=TokenUsage()
            )
        reference = next(key for key in agent._metabase_evidence.bindings if key.endswith(":r0c0}}"))
        return LLMResponse(text_blocks=["Matching orders: " + reference], tool_use_blocks=[], usage=TokenUsage())

    async def stream(**kwargs):
        value = await response(**kwargs)
        for text in value.text_blocks:
            yield "text", text
        yield "response", value

    adapter = MagicMock()
    adapter.create_message = response
    adapter.stream_message = stream
    adapter.build_assistant_message.side_effect = lambda r: {"role": "assistant", "content": "\n".join(r.text_blocks)}
    adapter.build_tool_result_message.side_effect = lambda content: {"role": "user", "content": content}
    dispatch = AsyncMock(side_effect=[result([[1001], [1002]], aggregate=False), result([[65]])])
    with (
        patch("app.services.policy_service.get_active_policy", new=AsyncMock(return_value=None)),
        patch("app.services.chat.mutation_guard.classify_connector_mutation", new=AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", new=dispatch),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        if streaming:
            events = [
                event
                async for event in BaseSpecialistAgent.run_streaming(
                    agent, "Count orders", {}, AsyncMock(), adapter, "test"
                )
            ]
            assert "".join(payload for kind, payload in events if kind == "text") == "Matching orders: 65"
            answer = events[-1][1]
        else:
            answer = await BaseSpecialistAgent.run(agent, "Count orders", {}, AsyncMock(), adapter, "test")
    assert answer.data == "Matching orders: 65"
    assert dispatch.await_count == 2 and len(calls) == 4
    assert "Unverified numerical text" in json.dumps(calls[2])


@pytest.mark.parametrize("streaming", [True, False])
async def test_budget_exhaustion_never_releases_unverified_numeric_text(streaming):
    connector = _connector(names=["query"])
    agent = _agent([connector])
    agent._metabase_evidence = MetabaseEvidence({f"ext__{connector.id.hex}__query"})
    value = LLMResponse(text_blocks=["44 orders"], tool_use_blocks=[], usage=TokenUsage())

    async def stream(**kwargs):
        yield "text", "44 orders"
        yield "response", value

    adapter = MagicMock()
    adapter.create_message = AsyncMock(return_value=value)
    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": "44 orders"}
    with (
        patch("app.services.policy_service.get_active_policy", new=AsyncMock(return_value=None)),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        if streaming:
            events = [
                event
                async for event in BaseSpecialistAgent.run_streaming(
                    agent, "Count orders", {}, AsyncMock(), adapter, "test"
                )
            ]
            assert "".join(payload for kind, payload in events if kind == "text") == UNVERIFIED
            answer = events[-1][1]
        else:
            answer = await BaseSpecialistAgent.run(agent, "Count orders", {}, AsyncMock(), adapter, "test")
    assert answer.data == UNVERIFIED and agent._numeric_verification_failed


def test_native_saved_question_can_render_its_actual_result_without_scalar_proof():
    name = TOOL.replace("__query", "__execute_question")
    evidence = MetabaseEvidence({name})
    raw = {
        "status": "completed",
        "json_query": {
            "lib/type": "mbql/query",
            "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT COUNT(*) AS count FROM orders"}],
        },
        "data": {"cols": [{"name": "count", "source": "native"}], "rows": [[65]]},
    }
    output = json.loads(evidence.observe(name, {"id": 5248}, json.dumps(raw)))
    assert not output["server_aggregate"]
    answer = "Saved question result:\n" + output["table_reference"]
    assert evidence.feedback(answer) is None
    assert "| count |" in evidence.resolve(answer) and "| 65 |" in evidence.resolve(answer)
    assert evidence.feedback("There are 65 matching orders.")


def calculate(evidence, left, right, operation="percentage", **kwargs):
    return evidence.calculate({"operation": operation, "left_reference": left, "right_reference": right, **kwargs})


def test_cancellation_percentage_is_calculated_from_controlled_aggregate_references():
    evidence = MetabaseEvidence({TOOL})
    states = observed(evidence, [["canceled", 24], ["complete", 41]], grouped=True)
    total = observed(evidence, [[65]])
    numerator, denominator = states["rows"][0][1], total["rows"][0][0]
    output = calculate(evidence, numerator, denominator)
    answer = "Cancellation rate: " + output["value_reference"] + "%"
    assert evidence.feedback(answer) is None
    assert evidence.resolve(answer) == "Cancellation rate: 36.92%"
    assert output["unit"] == "percent"
    assert evidence.feedback("Canceled %: " + numerator + " / " + denominator)


@pytest.mark.parametrize(
    "operation,left,right,expected",
    [
        ("percentage", 0, 65, "0.00"),
        ("ratio", 10, 4, "2.50"),
        ("difference", 41, 24, "17.00"),
        ("percentage_change", 80, 100, "-20.00"),
        ("difference", 0, 0, "0.00"),
    ],
)
def test_supported_calculations_preserve_sign_and_zero(operation, left, right, expected):
    evidence = MetabaseEvidence({TOOL})
    lref = observed(evidence, [[left]])["rows"][0][0]
    rref = observed(evidence, [[right]])["rows"][0][0]
    output = calculate(evidence, lref, rref, operation)
    assert evidence.resolve(output["value_reference"]) == expected


@pytest.mark.parametrize("operation", ["percentage", "ratio", "percentage_change"])
def test_zero_denominator_is_unavailable_not_a_fabricated_zero(operation):
    evidence = MetabaseEvidence({TOOL})
    zero = observed(evidence, [[0]])["rows"][0][0]
    output = calculate(evidence, zero, zero, operation)
    assert "undefined" in output["error"] and "value_reference" not in output


def test_calculator_rejects_detail_table_control_unknown_and_other_turn_references():
    evidence = MetabaseEvidence({TOOL})
    detail = observed(evidence, [[24]], params=query(aggregate=False), aggregate=False)
    total = observed(evidence, [[65]])["rows"][0][0]
    foreign = observed(MetabaseEvidence({TOOL}), [[24]])["rows"][0][0]
    for invalid in ["24", detail["table_reference"], "{{mb_ref:invented}}", foreign]:
        assert "error" in calculate(evidence, invalid, total)
    assert not evidence.calculation_dependencies


@pytest.mark.parametrize(
    "kwargs", [{"decimal_places": -1}, {"decimal_places": 7}, {"decimal_places": True}, {"code": "1/2"}]
)
def test_calculator_rejects_invalid_precision_or_extra_expression_input(kwargs):
    evidence = MetabaseEvidence({TOOL})
    ref = observed(evidence, [[65]])["rows"][0][0]
    assert "error" in calculate(evidence, ref, ref, **kwargs)


def test_calculation_requires_controls_for_both_inputs_and_rechecks_dependencies():
    evidence = MetabaseEvidence({TOOL})
    first = observed(evidence, [["canceled", 24]], grouped=True)["rows"][0][1]
    second = observed(evidence, [["canceled", 12]], grouped=True, params=query(grouped=True, batch=396))["rows"][0][1]
    assert "error" in calculate(evidence, first, second)
    observed(evidence, [[24]])
    assert "error" in calculate(evidence, first, second)
    observed(evidence, [[12]], params=query(batch=396))
    output = calculate(evidence, first, second, "percentage_change")
    assert evidence.resolve(output["value_reference"]) == "100.00"
    observed(evidence, [[1]], params=query(batch=396))
    assert evidence.feedback(output["value_reference"])


def test_chained_calculation_uses_full_precision_and_keeps_lineage():
    evidence = MetabaseEvidence({TOOL})
    one = observed(evidence, [[1]])["rows"][0][0]
    three = observed(evidence, [[3]])["rows"][0][0]
    ratio = calculate(evidence, one, three, "ratio")["value_reference"]
    assert evidence.resolve(ratio) == "0.33"
    percent = calculate(evidence, ratio, one)["value_reference"]
    assert evidence.resolve(percent) == "33.33"
    assert evidence.calculation_dependencies[percent] == evidence.calculation_dependencies[ratio]


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("policy_allowed", [True, False])
async def test_calculator_runs_inside_the_agent_without_external_dispatch(streaming, policy_allowed):
    from app.services.chat.source_selection import SourceSelection

    connector = _connector(names=["query"])
    agent = _agent([connector])
    agent._request_kind = "analytics"
    agent._configure_metabase_evidence(SourceSelection(selected_sources=("metabase",)))
    assert "analytics_calculate" in [t["name"] for t in agent.tool_definitions]
    name = f"ext__{connector.id.hex}__query"
    numerator = observed(agent._metabase_evidence, [[24]], tool=name)["rows"][0][0]
    denominator = observed(agent._metabase_evidence, [[65]], tool=name)["rows"][0][0]
    calls = 0

    async def response(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(
                tool_use_blocks=[
                    ToolUseBlock(
                        "calculate",
                        "analytics_calculate",
                        {"operation": "percentage", "left_reference": numerator, "right_reference": denominator},
                    )
                ]
            )
        if not policy_allowed:
            return LLMResponse(text_blocks=["Calculation blocked by workspace policy."])
        ref = next(iter(agent._metabase_evidence.calculation_dependencies))
        return LLMResponse(text_blocks=["Canceled: " + ref + "%"])

    async def stream(**kwargs):
        yield "response", await response(**kwargs)

    adapter = MagicMock()
    adapter.create_message = response
    adapter.stream_message = stream
    adapter.build_assistant_message.side_effect = lambda r: {"role": "assistant", "content": ""}
    adapter.build_tool_result_message.side_effect = lambda content: {"role": "user", "content": content}
    expected = "Canceled: 36.92%" if policy_allowed else "Calculation blocked by workspace policy."
    with (
        patch(
            "app.services.policy_service.evaluate_tool_call",
            return_value={"allowed": policy_allowed, "reason": "test policy"},
        ),
        patch("app.services.policy_service.get_active_policy", new=AsyncMock(return_value=None)),
        patch("app.services.chat.mutation_guard.classify_connector_mutation", new=AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", new=AsyncMock()) as dispatch,
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        if streaming:
            events = [
                e
                async for e in BaseSpecialistAgent.run_streaming(
                    agent, "Calculate cancellation rate", {}, AsyncMock(), adapter, "test"
                )
            ]
            answer = events[-1][1]
            assert "".join(p for k, p in events if k == "text") == expected
        else:
            answer = await BaseSpecialistAgent.run(
                agent, "Calculate cancellation rate", {}, AsyncMock(), adapter, "test"
            )
        dispatch.assert_not_awaited()
    assert answer.data == expected
    assert answer.tool_calls_log[0]["tool"] == "analytics_calculate"
    assert bool(agent._metabase_evidence.calculation_dependencies) is policy_allowed
    agent._reset_source_routing()
    assert "analytics_calculate" not in [t["name"] for t in agent.tool_definitions]
