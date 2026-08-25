"""Unit tests for mcp_client_service's per-tool timeout policy.

ns_getRecordTypeMetadata was controller-verified live (2026-08-25) to take
11-18s against a 15.0s ceiling — a coin-flip failure that silently breaks
both (C)'s ask_user name-verification and (A)'s pre-composition investigation
gate, both of which depend on this call succeeding.
"""

from app.services.mcp_client_service import _tool_timeout_seconds


def test_get_record_type_metadata_gets_the_60s_ceiling():
    assert _tool_timeout_seconds("ns_getRecordTypeMetadata") == 60.0


def test_report_and_search_and_suiteql_keep_the_60s_ceiling():
    assert _tool_timeout_seconds("ns_runReport") == 60.0
    assert _tool_timeout_seconds("ns_runSavedSearch") == 60.0
    assert _tool_timeout_seconds("ns_runCustomSuiteQL") == 60.0


def test_ordinary_tools_keep_the_15s_ceiling():
    assert _tool_timeout_seconds("ns_getSubsidiaries") == 15.0
    assert _tool_timeout_seconds("ns_createRecord") == 15.0
