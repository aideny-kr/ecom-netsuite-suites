"""query_export must read the row cap from Settings, not a hardcoded literal.

Lock-in: the chat-UI display cap, the export endpoint, and the global
NETSUITE_SUITEQL_MAX_ROWS need to stay in sync. If someone bumps the global
again (e.g. 50000 -> 100000), the export endpoint should follow without a
second edit.
"""

import inspect

from app.api.v1 import exports


def test_query_export_uses_settings_cap_not_literal():
    src = inspect.getsource(exports.query_export)
    assert "settings.NETSUITE_SUITEQL_MAX_ROWS" in src, (
        "query_export must use settings.NETSUITE_SUITEQL_MAX_ROWS so the "
        "export cap stays in lockstep with the global row cap."
    )
    assert "50_000" not in src, "no hardcoded 50_000 literal in query_export"
    assert "limit=50000" not in src, "no hardcoded limit=50000 in query_export"
