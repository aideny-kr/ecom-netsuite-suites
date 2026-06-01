"""CI-safe tests for `desktop/runtime/mcp-servers/ns-suiteql/`.

TDD red phase per /goal #3 plan: failing tests for the SuiteQL MCP server
BEFORE writing implementation.

Mocks:
- httpx.AsyncClient (no live NetSuite traffic in CI)
- Filesystem (uses tmp_path for `~/SuiteStudio/{org}/netsuite-connection.json`)

Contract being asserted (per plan §Goal + gate #1):
- `netsuite_client.is_read_only_sql()` rejects non-SELECT and embedded writes
- `netsuite_client.enforce_limit()` injects FETCH FIRST when missing, caps
  existing limits above the configured max
- `netsuite_client.normalize_account_id()` lowercases + dashes underscores
- `netsuite_client.load_connection()` reads JSON, refuses placeholder values
  and missing files with a structured error (no raise — surface to LLM)
- `netsuite_client.run_query()` (async) POSTs to the right URL, sends
  Authorization: Bearer header, returns columns+rows shape on 200,
  structured error on 401/HTTP error / network error
- `server.ns_runSuiteQL` (FastMCP tool function) is the thin wrapper that
  delegates to `netsuite_client.run_query` and is registered on `mcp`
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

# Make ns-suiteql modules importable without packaging them.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(
    0,
    os.path.join(HERE, os.pardir, "runtime", "mcp-servers", "ns-suiteql"),
)

import netsuite_client  # noqa: E402  (path-augmented import)
import server  # noqa: E402  (path-augmented import)


# ---------------------------------------------------------------------------
# Query validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id, name FROM subsidiary",
        "  select * from customer  ",
        "SELECT t.id FROM transaction t WHERE t.trandate >= TRUNC(SYSDATE)",
    ],
)
def test_is_read_only_sql_accepts_select(query):
    assert netsuite_client.is_read_only_sql(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO subsidiary VALUES (1, 'x')",
        "UPDATE customer SET email = 'x' WHERE id = 1",
        "DELETE FROM subsidiary",
        "DROP TABLE subsidiary",
        "ALTER TABLE customer ADD COLUMN x",
        "TRUNCATE TABLE subsidiary",
        "CREATE TABLE foo (id INT)",
        "SELECT id FROM subsidiary; DELETE FROM customer",
    ],
)
def test_is_read_only_sql_rejects_writes(query):
    assert netsuite_client.is_read_only_sql(query) is False


# ---------------------------------------------------------------------------
# Limit injection
# ---------------------------------------------------------------------------


def test_enforce_limit_appends_fetch_first_when_missing():
    out = netsuite_client.enforce_limit("SELECT id FROM subsidiary", 100)
    assert "FETCH FIRST 100 ROWS ONLY" in out
    assert out.upper().count("FETCH FIRST") == 1


def test_enforce_limit_respects_existing_lower_fetch_first():
    out = netsuite_client.enforce_limit(
        "SELECT id FROM subsidiary FETCH FIRST 5 ROWS ONLY", 100
    )
    assert "FETCH FIRST 5 ROWS ONLY" in out
    assert "FETCH FIRST 100" not in out


def test_enforce_limit_caps_existing_higher_fetch_first():
    out = netsuite_client.enforce_limit(
        "SELECT id FROM subsidiary FETCH FIRST 9999 ROWS ONLY", 100
    )
    assert "FETCH FIRST 100 ROWS ONLY" in out
    assert "9999" not in out


# ---------------------------------------------------------------------------
# Account ID normalization (suitetalk URL constraint)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TSTDRV1234567", "tstdrv1234567"),
        ("1234567_SB1", "1234567-sb1"),
        ("ACME_PROD", "acme-prod"),
        ("already-lower-dashed", "already-lower-dashed"),
    ],
)
def test_normalize_account_id(raw, expected):
    assert netsuite_client.normalize_account_id(raw) == expected


# ---------------------------------------------------------------------------
# Connection file loading
# ---------------------------------------------------------------------------


def _write_connection(tmp_path, payload):
    path = tmp_path / "netsuite-connection.json"
    path.write_text(json.dumps(payload))
    return path


def test_load_connection_returns_creds_on_happy_path(tmp_path):
    path = _write_connection(
        tmp_path,
        {
            "account_id": "TSTDRV1234567",
            "bearer_token": "eyJrealtoken",
            "expires_at": "2026-12-31T00:00:00Z",
        },
    )
    creds = netsuite_client.load_connection(str(path))
    assert creds["account_id"] == "TSTDRV1234567"
    assert creds["bearer_token"] == "eyJrealtoken"


def test_load_connection_returns_error_when_path_missing(tmp_path):
    missing = tmp_path / "absent.json"
    err = netsuite_client.load_connection(str(missing))
    assert err["error"] is True
    assert "netsuite-connection.json" in err["message"].lower() or "not found" in err["message"].lower()


def test_load_connection_returns_error_on_placeholder_token(tmp_path):
    # The sidecar writes this exact placeholder; the server must refuse to
    # send it as a Bearer token, returning a structured error the agent can
    # surface to the operator.
    path = _write_connection(
        tmp_path,
        {
            "account_id": "REPLACE_ME",
            "bearer_token": "REPLACE_ME",
            "expires_at": "REPLACE_ME",
        },
    )
    err = netsuite_client.load_connection(str(path))
    assert err["error"] is True
    assert "placeholder" in err["message"].lower() or "REPLACE_ME" in err["message"]


# ---------------------------------------------------------------------------
# run_query — the HTTP call (mocked via httpx MockTransport)
# ---------------------------------------------------------------------------


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_run_query_posts_to_suitetalk_url_with_bearer_token(monkeypatch, tmp_path):
    path = _write_connection(
        tmp_path,
        {
            "account_id": "TSTDRV1234567",
            "bearer_token": "eyJrealtoken",
            "expires_at": "2026-12-31T00:00:00Z",
        },
    )
    monkeypatch.setenv("SUITE_STUDIO_NS_CONNECTION_FILE", str(path))

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "items": [{"id": 1, "name": "USA"}, {"id": 2, "name": "Switzerland"}],
                "totalResults": 2,
            },
        )

    result = await netsuite_client.run_query(
        "SELECT id, name FROM subsidiary",
        transport=_mock_transport(handler),
    )

    assert captured["url"] == "https://tstdrv1234567.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    assert captured["auth"] == "Bearer eyJrealtoken"
    assert captured["content_type"] == "application/json"
    assert "FETCH FIRST" in captured["body"]["q"]
    assert result["row_count"] == 2
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[1, "USA"], [2, "Switzerland"]]


@pytest.mark.asyncio
async def test_run_query_rejects_write_without_calling_netsuite(monkeypatch, tmp_path):
    path = _write_connection(
        tmp_path,
        {
            "account_id": "TSTDRV1234567",
            "bearer_token": "eyJrealtoken",
            "expires_at": "2026-12-31T00:00:00Z",
        },
    )
    monkeypatch.setenv("SUITE_STUDIO_NS_CONNECTION_FILE", str(path))

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"items": []})

    result = await netsuite_client.run_query(
        "DELETE FROM subsidiary",
        transport=_mock_transport(handler),
    )

    assert result["error"] is True
    assert "read-only" in result["message"].lower() or "select" in result["message"].lower()
    assert calls == [], "Write queries must be rejected BEFORE any HTTP call"


@pytest.mark.asyncio
async def test_run_query_returns_structured_error_on_401(monkeypatch, tmp_path):
    path = _write_connection(
        tmp_path,
        {
            "account_id": "TSTDRV1234567",
            "bearer_token": "eyJrealtoken",
            "expires_at": "2026-12-31T00:00:00Z",
        },
    )
    monkeypatch.setenv("SUITE_STUDIO_NS_CONNECTION_FILE", str(path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"o:errorDetails": [{"detail": "Invalid login"}]})

    result = await netsuite_client.run_query(
        "SELECT id FROM subsidiary",
        transport=_mock_transport(handler),
    )

    assert result["error"] is True
    assert "401" in result["message"] or "unauthorized" in result["message"].lower()


@pytest.mark.asyncio
async def test_run_query_returns_error_when_connection_file_env_unset(monkeypatch):
    monkeypatch.delenv("SUITE_STUDIO_NS_CONNECTION_FILE", raising=False)

    result = await netsuite_client.run_query("SELECT id FROM subsidiary")

    assert result["error"] is True
    assert "SUITE_STUDIO_NS_CONNECTION_FILE" in result["message"]


# ---------------------------------------------------------------------------
# FastMCP server registration
# ---------------------------------------------------------------------------


def test_server_module_exposes_fastmcp_instance():
    """server.py must construct an `mcp` FastMCP instance at module load."""
    assert hasattr(server, "mcp"), "server.py must expose `mcp` (the FastMCP instance)"
    from mcp.server.fastmcp import FastMCP
    assert isinstance(server.mcp, FastMCP)


def test_server_registers_ns_runSuiteQL_tool():
    """ns_runSuiteQL must be a registered MCP tool on the server."""
    tool_names = _list_registered_tools(server.mcp)
    assert "ns_runSuiteQL" in tool_names, f"expected ns_runSuiteQL among {tool_names}"


def _list_registered_tools(mcp_instance) -> list[str]:
    """Probe a FastMCP instance for its registered tool names.

    The internal API has shifted across mcp SDK versions; try the known
    shapes until one yields a list.
    """
    # mcp 1.x: FastMCP keeps a ToolManager at `_tool_manager` whose
    # `list_tools()` returns Tool objects with `.name`.
    tm = getattr(mcp_instance, "_tool_manager", None)
    if tm is not None and hasattr(tm, "list_tools"):
        tools = tm.list_tools()
        return [getattr(t, "name", str(t)) for t in tools]
    # Fallback: walk the public registry attr if present.
    tools = getattr(mcp_instance, "tools", None)
    if tools is not None:
        return [getattr(t, "name", str(t)) for t in tools]
    raise AssertionError("Could not enumerate FastMCP tools — SDK shape changed?")
