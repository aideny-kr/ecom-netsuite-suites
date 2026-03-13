"""NetSuite native report tool — runs financial reports via MCP ns_runReport.

Stub module. Full implementation pending.
"""

import json


async def execute(*, tenant_id: str, actor_id: str, correlation_id: str | None = None, db=None, **params) -> str:
    """Execute a native NetSuite report via MCP."""
    return json.dumps({"success": False, "error": "netsuite.report tool not yet implemented"})
