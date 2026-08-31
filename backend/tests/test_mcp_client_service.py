"""Unit tests for mcp_client_service's per-tool timeout policy.

There is deliberately no short tier any more. The old policy gave 60s to four
named READ tools and 15s to everything else — which meant every irreversible
WRITE ran on the shortest budget in the system. That is backwards on the risk
axis: a read that times out costs a retry, a write that times out costs an
orphaned record nobody can see, because a client-side timeout says nothing
about whether NetSuite committed.

Proven live on 2026-08-27, sandbox 6738075-sb1: ns_createRecord exceeded 15s,
the customer WAS created (internal id 5264348), and the app recorded the write
as failed and offered to run the identical payload again. The sandbox is
simply slower than production (12.4s vs 2.8s on the same SuiteQL) — production
is not immune, only luckier.

ns_getRecordTypeMetadata was controller-verified live (2026-08-25) at 11-18s,
which is what earned the 60s tier in the first place.
"""

from app.services.mcp_client_service import _tool_timeout_seconds


def test_reads_keep_the_60s_ceiling():
    assert _tool_timeout_seconds("ns_getRecordTypeMetadata") == 60.0
    assert _tool_timeout_seconds("ns_runReport") == 60.0
    assert _tool_timeout_seconds("ns_runSavedSearch") == 60.0
    assert _tool_timeout_seconds("ns_runCustomSuiteQL") == 60.0


def test_writes_get_the_full_ceiling():
    """The regression this file exists for: a create/update must never run on
    a shorter budget than a report query."""
    assert _tool_timeout_seconds("ns_createRecord") == 60.0
    assert _tool_timeout_seconds("ns_updateRecord") == 60.0


def test_no_write_runs_on_a_shorter_budget_than_a_read():
    assert _tool_timeout_seconds("ns_createRecord") >= _tool_timeout_seconds("ns_runCustomSuiteQL")


def test_a_tool_nobody_has_seen_yet_cannot_inherit_a_short_budget():
    """The NetSuite tool surface is discovered at runtime from Oracle's MCP
    server, so an allow-list of names cannot be complete. A tool that ships
    tomorrow must not get a budget short enough to orphan a write — same
    fail-closed reasoning as _NETSUITE_READ_ONLY_TOOLS in chat/tools.py."""
    assert _tool_timeout_seconds("ns_someToolOracleShipsTomorrow") == 60.0
    assert _tool_timeout_seconds("ns_getSubsidiaries") == 60.0
