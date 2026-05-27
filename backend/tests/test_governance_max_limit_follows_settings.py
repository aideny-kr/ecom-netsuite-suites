"""governance.TOOL_CONFIGS clamps must follow NETSUITE_SUITEQL_MAX_ROWS.

Regression guard for the bug codex caught: the governance layer clamps
LLM-supplied `limit` BEFORE netsuite_suiteql.execute() reads
settings.NETSUITE_SUITEQL_MAX_ROWS. If max_limit is left at a literal,
the global Settings cap becomes dead code for local SuiteQL chat queries.
"""

from app.core.config import settings
from app.mcp.governance import TOOL_CONFIGS


def test_netsuite_suiteql_max_limit_matches_settings():
    # Extract both sides to ints first — putting `settings.X` directly in an
    # assert dumps the full Settings repr (including secrets) into the test
    # log on failure. See feedback_pydantic_settings_assertion_leak.md.
    expected = settings.NETSUITE_SUITEQL_MAX_ROWS
    actual = TOOL_CONFIGS["netsuite.suiteql"]["max_limit"]
    assert actual == expected


def test_netsuite_suiteql_stub_max_limit_matches_settings():
    # The stub variant must move in lockstep — tests + dev paths shouldn't
    # silently use a different ceiling than production.
    expected = settings.NETSUITE_SUITEQL_MAX_ROWS
    actual = TOOL_CONFIGS["netsuite.suiteql_stub"]["max_limit"]
    assert actual == expected


def test_netsuite_suiteql_max_limit_is_50k_or_more():
    # Belt-and-suspenders: even if someone reverts Settings, we want a
    # loud test failure rather than a silent clamp-back to 1000.
    actual = TOOL_CONFIGS["netsuite.suiteql"]["max_limit"]
    assert actual >= 50000
