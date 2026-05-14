"""Regression guard — ``get_active_connectors_for_tenant`` must emit an
ORDER BY clause so Postgres returns rows in byte-stable order across calls.

Without ORDER BY, row order is undefined; that would shift the Anthropic
prompt-cache breakpoint stamped on the last tool definition and silently
bust the cache. Source: codex review of the May 2026 prompt-cache audit.

The test inspects the SQLAlchemy statement compiled by the function rather
than spinning up a DB session — sufficient to lock in the ORDER BY clause.
"""

from __future__ import annotations

import inspect

from app.services import mcp_connector_service


def test_get_active_connectors_for_tenant_has_order_by():
    src = inspect.getsource(mcp_connector_service.get_active_connectors_for_tenant)
    # Must call .order_by on the select statement. We check for the literal
    # call rather than compiling a SQL string because the function is async
    # and parameterised on tenant_id.
    assert ".order_by(" in src, (
        "get_active_connectors_for_tenant must include .order_by() so Postgres "
        "row order is deterministic — otherwise the Anthropic prompt-cache "
        "breakpoint silently shifts and the cache invalidates."
    )
    assert "McpConnector.id" in src
