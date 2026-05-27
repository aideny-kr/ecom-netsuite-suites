"""Default for the global SuiteQL row cap.

Locks in the bump 1000 -> 50000 across Settings, the PolicyProfile model
column default, and the evaluate_tool_call fallback message.
"""

from types import SimpleNamespace

from app.core.config import Settings
from app.models.policy_profile import PolicyProfile
from app.services.policy_service import evaluate_tool_call


def test_settings_netsuite_suiteql_max_rows_default_is_50000():
    # Extract the int first — pytest's assertion rewrite calls repr() on both
    # sides when this fails, which would dump the entire Settings object
    # (including any populated secrets) into the test log.
    value = Settings().NETSUITE_SUITEQL_MAX_ROWS
    assert value == 50000


def test_policy_profile_column_default_max_rows_is_50000():
    column = PolicyProfile.__table__.c.max_rows_per_query
    assert column.default.arg == 50000


def test_evaluate_tool_call_uses_50000_when_policy_max_rows_unset():
    # Policy exists, require_row_limit=True, but max_rows_per_query is None
    # (e.g. created via a path that didn't supply it). The fallback message
    # the user sees must instruct them with the new cap, not the old 1000.
    policy = SimpleNamespace(
        tool_allowlist=None,
        allowed_record_types=None,
        blocked_fields=None,
        require_row_limit=True,
        max_rows_per_query=None,
    )
    result = evaluate_tool_call(policy, "external_mcp_query", {"query": "SELECT * FROM foo"})
    assert result["allowed"] is False
    assert "50000" in result["reason"]
    assert "1000" not in result["reason"]
