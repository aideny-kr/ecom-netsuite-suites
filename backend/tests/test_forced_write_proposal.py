"""The forced write-proposal composition hop.

WHY THIS EXISTS, measured rather than argued. The confirmation card only ever
appears when the model *volunteers* to call ns_createRecord. Driven live on
staging 2026-08-28 with one identical prompt ("create a test customer called
... in the sandbox"), three runs: 1 reached a card, 2 answered in prose and
ended the turn. Two operator runs minutes earlier had both ended in prose. So
the shipped feature works about a third of the time, and the other two thirds
the operator is asked a question in chat that they cannot act on — the card,
the slot dropdown and the whole HITL surface are simply absent.

Prompt text has now failed at this five times (three observed instances that
motivated the stage-1 bounce, plus these two). `.claude/rules/agent-graph.md`
is explicit: guardrails are code at the choke point, never prompt prose — and
a bounce delivered as a tool result IS prompt prose. So stage 2 stops asking.

The hop offers the model ONLY the write tools of the connector it already
investigated, with tool_choice forcing a call. It cannot answer in chat
because chat is not on the menu; it still picks the operation (create vs
update) and composes the payload, so nothing about the write is decided here.
"""

from app.services.chat.agents.base_agent import _forced_write_tool_subset

CONN_A = "7ca2433dae3f403b88b410521e8e7007"  # the sandbox connector
CONN_B = "fc1cba33e9924f62a5b7df0d5f235214"  # production


def _tools(*names):
    return [{"name": n, "description": "", "input_schema": {}} for n in names]


ALL_TOOLS = _tools(
    f"ext__{CONN_A}__ns_createRecord",
    f"ext__{CONN_A}__ns_updateRecord",
    f"ext__{CONN_A}__ns_runCustomSuiteQL",
    f"ext__{CONN_A}__ns_getRecordTypeMetadata",
    f"ext__{CONN_B}__ns_createRecord",
    "netsuite_suiteql",
)


def _log(connector, tool="ns_getRecordTypeMetadata", record_type="customer"):
    return [{"tool": f"ext__{connector}__{tool}", "params": {"recordType": record_type}}]


def test_offers_only_write_tools():
    """A read tool on the menu is an escape hatch — the model took it live."""
    names = {t["name"] for t in _forced_write_tool_subset(_log(CONN_A), ALL_TOOLS)}
    assert names == {f"ext__{CONN_A}__ns_createRecord", f"ext__{CONN_A}__ns_updateRecord"}


def test_never_offers_another_connectors_write_tools():
    """The account binding is the whole point: forcing a write must not hand
    the model production's create tool when it investigated sandbox."""
    names = {t["name"] for t in _forced_write_tool_subset(_log(CONN_A), ALL_TOOLS)}
    assert f"ext__{CONN_B}__ns_createRecord" not in names


def test_both_operations_stay_available():
    """Create vs update is the model's call, not ours — the metadata lookup
    says which record TYPE, never which operation."""
    names = {t["name"] for t in _forced_write_tool_subset(_log(CONN_A), ALL_TOOLS)}
    assert f"ext__{CONN_A}__ns_updateRecord" in names


def test_delete_is_never_on_the_forced_menu():
    """Forcing means the model MUST pick something from this list. A spurious
    delete proposal from a create request is a far worse failure than not
    forcing at all, and deletes are rare enough that the normal (unforced)
    path can carry them — the model can still call ns_deleteRecord itself."""
    with_delete = _tools(
        f"ext__{CONN_A}__ns_createRecord",
        f"ext__{CONN_A}__ns_deleteRecord",
    )
    names = {t["name"] for t in _forced_write_tool_subset(_log(CONN_A), with_delete)}
    assert names == {f"ext__{CONN_A}__ns_createRecord"}


def test_a_connector_offering_only_delete_forces_nothing():
    only_delete = _tools(f"ext__{CONN_A}__ns_deleteRecord")
    assert _forced_write_tool_subset(_log(CONN_A), only_delete) == []


def test_no_metadata_call_means_no_forcing():
    """No declared write intent, no forced write. This is the guard against
    forcing a proposal onto someone who only asked a question."""
    assert _forced_write_tool_subset([], ALL_TOOLS) == []


def test_a_connector_with_no_write_tools_forces_nothing():
    """Returning [] must mean 'do not force' — an empty tool list with a
    forced tool_choice is an API error, and a read-only connector is real."""
    read_only = _tools(f"ext__{CONN_A}__ns_runCustomSuiteQL")
    assert _forced_write_tool_subset(_log(CONN_A), read_only) == []


def test_unparseable_tool_names_are_skipped_not_crashed():
    assert _forced_write_tool_subset(_log(CONN_A), _tools("netsuite_suiteql", "bigquery_sql")) == []
