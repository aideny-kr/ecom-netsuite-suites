# `ns-suiteql` — Suite Studio MCP server

> Read-only SuiteQL execution against operator's NetSuite tenant. Exposes a single MCP tool, `ns_runSuiteQL(query)`, over **stdio** transport.

## Why this server exists

Wired into the Suite Studio Desktop sidecar (`desktop/runtime/sidecar.py`) per the third `/goal` (plan: `docs/superpowers/plans/2026-05-25-desktop-b0-mcp-suiteql.md`). Establishes the Hermes Agent MCP-client transport — the first time the Suite Studio runtime calls outward through MCP into a Suite-Studio-authored server.

This is the **smallest viable NetSuite MCP surface** — exactly one read-only tool. Additional tools (`ns_runReport`, `ns_runSavedSearch`, `ns_getRecord`, …) land in subsequent `/goal`s. Mutation tools (`ns_createRecord`, `ns_updateRecord`) require the HITL flow scheduled for the sixth `/goal`.

## Run command (used by the sidecar to spawn the subprocess)

```
python -m server
```

…invoked with `cwd = desktop/runtime/mcp-servers/ns-suiteql/` so the relative `server` module resolves.

## Configuration — `~/SuiteStudio/{org}/netsuite-connection.json`

Operator-populated JSON file. The sidecar auto-creates a placeholder template on first run; the operator fills it in out-of-band. **Do NOT paste a Bearer token in agent prompts or commit it to the repo.**

```json
{
  "account_id": "TSTDRV1234567",
  "bearer_token": "eyJ...<your-OAuth2-access-token>...",
  "expires_at": "2026-12-31T00:00:00Z"
}
```

The server reads the file path from the `SUITE_STUDIO_NS_CONNECTION_FILE` env var (set by the sidecar). If the env var is unset or the file is missing/contains placeholder values, the server's tool returns a structured error explaining how to populate it — the agent surfaces the error, no crash.

Refresh-on-401 is deliberately deferred. When the token expires the operator re-mints out-of-band (via the existing backend's OAuth flow) and updates the file.

## Tool contract

```
ns_runSuiteQL(query: str) -> dict
```

Validates the query is read-only (SELECT-only), injects a `FETCH FIRST 100 ROWS ONLY` cap if missing, POSTs to `https://{account_id}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql` with `Authorization: Bearer <token>`.

Returns `{"columns": [...], "rows": [[...], ...], "row_count": N, "truncated": bool, "query": <executed>}` on success, `{"error": True, "message": "..."}` on failure.

## SuiteQL dialect rules

See `desktop/skills/suite-studio-netsuite/suiteql/SKILL.md` for the dialect rules the agent loads alongside this server. Lifted verbatim from the Hosted backend's `netsuite.yaml` knowledge profile.
