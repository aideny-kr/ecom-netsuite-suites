"""ns-suiteql — FastMCP stdio server exposing a single tool: `ns_runSuiteQL`.

Spawned as a subprocess by the Suite Studio Desktop sidecar (see
`desktop/runtime/sidecar.py`). The sidecar registers this server with the
vendored Hermes Agent's MCP-client transport, which then surfaces the
tool to the AIAgent like any built-in tool.

Run command (the sidecar invokes this with cwd=mcp-servers/ns-suiteql/)::

    python -m server

Environment contract:
    SUITE_STUDIO_NS_CONNECTION_FILE — path to a JSON file with the operator's
        NetSuite OAuth 2.0 Bearer creds. The sidecar sets this to
        ~/SuiteStudio/{org}/netsuite-connection.json. Schema:
            {"account_id": "TSTDRV...", "bearer_token": "eyJ...",
             "expires_at": "2026-12-31T00:00:00Z"}

The body of the tool is intentionally one line — all behaviour lives in
`netsuite_client.run_query()` so the validation + HTTP path is testable
independently of the FastMCP wiring.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from netsuite_client import run_query

# Server identity — surfaced to the MCP client during handshake.
mcp = FastMCP("ns-suiteql")


@mcp.tool()
async def ns_runSuiteQL(query: str) -> dict:
    """Execute a read-only SuiteQL query against the operator's NetSuite tenant.

    SuiteQL is Oracle-based with NetSuite-specific behaviors. Consult
    `desktop/skills/suite-studio-netsuite/suiteql/SKILL.md` for dialect
    rules: pagination via `FETCH FIRST N ROWS ONLY` (never `LIMIT`),
    primary key is `id` (not `internalid`), date functions use Oracle
    syntax (`TRUNC(SYSDATE)`, `TO_DATE(...)`, never `CURRENT_DATE`),
    booleans are 'T'/'F' string literals, status codes are single-letter,
    etc.

    Smoke test query (gate #7 in plan /goal #3):

        SELECT id, name FROM subsidiary ORDER BY name

    Args:
        query: A read-only SELECT statement in SuiteQL.

    Returns:
        On success: {"columns": [...], "rows": [[...], ...], "row_count": N,
                     "truncated": bool, "query": <executed>, "limit": N}
        On failure: {"error": True, "message": "..."}
    """
    return await run_query(query)


if __name__ == "__main__":
    mcp.run()
