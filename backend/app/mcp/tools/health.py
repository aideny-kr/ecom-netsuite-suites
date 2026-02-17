from datetime import datetime, timezone


async def execute(params: dict, **kwargs) -> dict:
    """Health check tool — returns server status and tool count."""
    from app.mcp.registry import TOOL_REGISTRY

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tools_registered": len(TOOL_REGISTRY),
    }
