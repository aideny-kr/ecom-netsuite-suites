"""Unit tests for assemble_disclosure()."""

from datetime import timedelta

from app.services.chat.disclosure import assemble_disclosure


class _State:
    has_bigquery = True
    has_netsuite = True
    bq_healthy = True
    ns_healthy = True
    bq_sync_age = timedelta(hours=1)


def _tool_call(name, sql):
    return {"tool": name, "params": {"query": sql}, "success": True}


def test_assemble_returns_none_when_no_data_tool():
    result = assemble_disclosure(
        tool_calls=[],
        user_query="hello",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern=None,
        is_rerun=False,
    )
    assert result is None


def test_assemble_returns_none_for_fresh_proven_pattern():
    result = assemble_disclosure(
        tool_calls=[_tool_call("netsuite_suiteql", "SELECT COUNT(*) FROM transaction")],
        user_query="how many orders",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern={"age_days": 3},
        is_rerun=False,
    )
    assert result is None


def test_assemble_emits_for_stale_proven_pattern():
    result = assemble_disclosure(
        tool_calls=[_tool_call("netsuite_suiteql", "SELECT COUNT(*) FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'WW')")],
        user_query="how many orders this week",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern={"age_days": 10},
        is_rerun=False,
    )
    assert result is not None
    assert result.source == "netsuite"
    assert "week" in result.interpretation.lower()


def test_assemble_happy_path_suiteql():
    sql = "SELECT COUNT(*) FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'WW') AND type = 'SalesOrd'"
    result = assemble_disclosure(
        tool_calls=[_tool_call("netsuite_suiteql", sql)],
        user_query="how many sales orders this week",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern=None,
        is_rerun=False,
    )
    assert result is not None
    assert result.source == "netsuite"
    assert "week" in result.interpretation.lower()
    assert any("sales" in f.lower() or "salesord" in f.lower() for f in result.implicit_filters)
    assert result.can_switch_source is True  # orders = dual source
    assert result.is_rerun is False
    assert result.failure_mode is False


def test_assemble_rerun_flag_propagates():
    sql = "SELECT COUNT(*) FROM `dataset.orders` WHERE DATE(created_at) >= CURRENT_DATE()"
    result = assemble_disclosure(
        tool_calls=[_tool_call("bigquery_sql", sql)],
        user_query="how many orders today",
        current_source="bigquery",
        connector_state=_State(),
        matched_pattern=None,
        is_rerun=True,
    )
    assert result is not None
    assert result.source == "bigquery"
    assert result.is_rerun is True


def test_assemble_failure_mode_with_switch_hint():
    result = assemble_disclosure(
        tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": "SELECT ..."}, "success": False, "error": "OAuth token expired"}],
        user_query="top customers last month",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern=None,
        is_rerun=False,
    )
    assert result is not None
    assert result.failure_mode is True
    assert result.can_switch_source is True
    assert "tried" in result.interpretation.lower() or "netsuite" in result.interpretation.lower()
    assert result.implicit_filters == []


def test_assemble_failure_mode_without_switch_hint_returns_none():
    state = _State()
    state.has_bigquery = False
    result = assemble_disclosure(
        tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": "..."}, "success": False, "error": "boom"}],
        user_query="top customers last month",
        current_source="netsuite",
        connector_state=state,
        matched_pattern=None,
        is_rerun=False,
    )
    assert result is None


def test_assemble_picks_last_successful_tool_call():
    """When the agent makes multiple tool calls, we describe the LAST successful one."""
    result = assemble_disclosure(
        tool_calls=[
            _tool_call("netsuite_suiteql", "SELECT 1 FROM dual"),  # exploratory
            _tool_call("netsuite_suiteql", "SELECT COUNT(*) FROM transaction WHERE type = 'SalesOrd' AND trandate >= TRUNC(SYSDATE, 'MM')"),
        ],
        user_query="how many sales orders this month",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern=None,
        is_rerun=False,
    )
    assert result is not None
    assert "month" in result.interpretation.lower()
    assert any("sales" in f.lower() or "salesord" in f.lower() for f in result.implicit_filters)


def test_assemble_recognizes_prefixed_ext_mcp_tool_names():
    """External MCP tools are exposed to the LLM with an ext__{hex}__ prefix.
    The disclosure assembler must strip the prefix before matching against
    _DATA_TOOL_NAMES so production MCP-path queries still get a footer.
    """
    sql = "SELECT COUNT(*) FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'WW')"
    tool_calls = [
        {
            "tool": "ext__abcdef1234567890abcdef1234567890__ns_runCustomSuiteQL",
            "params": {"query": sql},
            "success": True,
        }
    ]
    result = assemble_disclosure(
        tool_calls=tool_calls,
        user_query="how many orders this week",
        current_source="netsuite",
        connector_state=_State(),
        matched_pattern=None,
        is_rerun=False,
    )
    assert result is not None
    assert result.source == "netsuite"
    assert "week" in result.interpretation.lower()
