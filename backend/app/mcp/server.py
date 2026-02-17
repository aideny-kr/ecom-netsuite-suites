"""
MCP Server — standalone process exposing AI-callable tools with governance.

Usage:
    python -m app.mcp.server
"""

import asyncio
import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.governance import governed_execute
from app.mcp.registry import TOOL_REGISTRY

logger = structlog.get_logger()


class MCPServer:
    """Simple MCP-compatible tool server with governance wrapper."""

    def __init__(self):
        self.tools = TOOL_REGISTRY

    def list_tools(self) -> list[dict]:
        """Return tool definitions for MCP discovery."""
        tools = []
        for name, tool in self.tools.items():
            tools.append(
                {
                    "name": name,
                    "description": tool["description"],
                    "inputSchema": {
                        "type": "object",
                        "properties": tool["params_schema"],
                    },
                }
            )
        return tools

    async def call_tool(
        self,
        tool_name: str,
        params: dict,
        tenant_id: str,
        actor_id: str | None = None,
        correlation_id: str | None = None,
        db: AsyncSession | None = None,
    ) -> dict:
        """Call a tool with full governance wrapper."""
        if tool_name not in self.tools:
            return {"error": f"Unknown tool: {tool_name}"}

        tool = self.tools[tool_name]
        result = await governed_execute(
            tool_name=tool_name,
            params=params,
            tenant_id=tenant_id,
            actor_id=actor_id,
            execute_fn=tool["execute"],
            correlation_id=correlation_id or str(uuid.uuid4()),
            db=db,
        )
        return result


# Singleton server instance
mcp_server = MCPServer()


async def main():
    """Run the MCP server in stdio mode for testing."""
    server = MCPServer()
    logger.info("mcp_server_started", tools=list(server.tools.keys()))

    # Simple stdin/stdout protocol for testing
    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, input)
            request = json.loads(line)

            method = request.get("method")
            if method == "tools/list":
                response = {"tools": server.list_tools()}
            elif method == "tools/call":
                result = await server.call_tool(
                    tool_name=request["params"]["name"],
                    params=request["params"].get("arguments", {}),
                    tenant_id=request["params"].get("tenant_id", ""),
                    actor_id=request["params"].get("actor_id"),
                    correlation_id=request["params"].get("correlation_id"),
                )
                response = {"result": result}
            else:
                response = {"error": f"Unknown method: {method}"}

            print(json.dumps(response), flush=True)
        except (EOFError, KeyboardInterrupt):
            break
        except Exception as e:
            print(json.dumps({"error": str(e)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
