"""Real MCP stdio integration tests for the desktop sidecar.

These tests intentionally do not patch ``AIAgent`` and do not use
``_StubAIAgent``. They spawn the actual MCP server processes through the
official MCP stdio client and assert the handshake + tool discovery work
without an Anthropic key.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import sidecar  # noqa: E402  (path-augmented import)


async def _list_tools_via_stdio(server_config: dict) -> set[str]:
    """Spawn a server with the same stdio transport Hermes Agent uses.

    Hermes Agent v2026.5.16 currently constructs ``StdioServerParameters``
    without passing the sidecar config's ``cwd`` field. Keep ``cwd`` unset
    here so this regression test catches server registrations that only work
    when the caller happens to chdir first.
    """
    params = StdioServerParameters(
        command=server_config["command"],
        args=server_config.get("args", []),
        env=server_config.get("env"),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_name", "expected_tools"),
    [
        ("ns-suiteql", {"ns_runSuiteQL"}),
        ("obsidian-memory", {"create_entities", "read_graph"}),
    ],
)
async def test_real_mcp_servers_connect_over_stdio_without_anthropic_key(
    monkeypatch,
    tmp_path,
    server_name: str,
    expected_tools: set[str],
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    sidecar.ensure_connection_template(org="default")
    sidecar.ensure_vault_scaffold(org="default")
    config = sidecar.build_mcp_server_config(org="default")

    tool_names = await asyncio.wait_for(
        _list_tools_via_stdio(config[server_name]),
        timeout=15,
    )

    missing = expected_tools - tool_names
    assert not missing, (
        f"{server_name} did not expose expected tools {sorted(missing)}; "
        f"discovered tools were {sorted(tool_names)}"
    )
