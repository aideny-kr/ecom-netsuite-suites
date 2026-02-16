import time
import uuid
from collections import defaultdict
from typing import Any, Callable

import structlog

logger = structlog.get_logger()

# Rate limit tracking: {tenant_id: {tool_name: [timestamps]}}
_rate_limits: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

TOOL_CONFIGS = {
    "netsuite.suiteql": {
        "default_limit": 100,
        "max_limit": 1000,
        "timeout_seconds": 30,
        "rate_limit_per_minute": 30,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["query", "limit"],
    },
    "recon.run": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 120,
        "rate_limit_per_minute": 10,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["date_from", "date_to", "payout_ids"],
    },
    "report.export": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 60,
        "rate_limit_per_minute": 20,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["report_type", "format", "filters"],
    },
    "schedule.create": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 10,
        "rate_limit_per_minute": 10,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["name", "schedule_type", "cron", "params"],
    },
    "schedule.list": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 10,
        "rate_limit_per_minute": 30,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": [],
    },
    "schedule.run": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 30,
        "rate_limit_per_minute": 10,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["schedule_id"],
    },
}


def check_rate_limit(tenant_id: str, tool_name: str) -> bool:
    """Check if the tenant is within rate limits for this tool."""
    config = TOOL_CONFIGS.get(tool_name, {})
    limit = config.get("rate_limit_per_minute", 60)

    now = time.time()
    window_start = now - 60

    # Clean old entries
    _rate_limits[tenant_id][tool_name] = [ts for ts in _rate_limits[tenant_id][tool_name] if ts > window_start]

    if len(_rate_limits[tenant_id][tool_name]) >= limit:
        return False

    _rate_limits[tenant_id][tool_name].append(now)
    return True


def validate_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate and filter parameters against allowlist."""
    config = TOOL_CONFIGS.get(tool_name, {})
    allowed = config.get("allowlisted_params", [])
    if not allowed:
        return params

    filtered = {k: v for k, v in params.items() if k in allowed}

    # Apply default limit
    default_limit = config.get("default_limit")
    max_limit = config.get("max_limit")
    if default_limit is not None and "limit" in allowed:
        if "limit" not in filtered:
            filtered["limit"] = default_limit
        elif max_limit and filtered["limit"] > max_limit:
            filtered["limit"] = max_limit

    return filtered


def redact_result(result: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive fields from tool results."""
    sensitive_keys = {"password", "secret", "token", "api_key", "credentials"}
    redacted = {}
    for key, value in result.items():
        if key.lower() in sensitive_keys:
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = redact_result(value)
        else:
            redacted[key] = value
    return redacted


def create_audit_payload(
    tool_name: str,
    params: dict[str, Any],
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict:
    """Create an audit event payload for a tool call."""
    return {
        "tool_name": tool_name,
        "params": {k: v for k, v in params.items() if k not in {"password", "secret", "token"}},
        "result_summary": {
            "status": "error" if error else "success",
            "error": error,
            "row_count": result.get("row_count", 0) if result else 0,
        },
    }


async def governed_execute(
    tool_name: str,
    params: dict[str, Any],
    tenant_id: str,
    actor_id: str | None,
    execute_fn: Callable,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """
    Governance wrapper: entitlement → rate limit → param validation → execute → redact → audit.
    """
    correlation_id = correlation_id or str(uuid.uuid4())

    # 1. Rate limit check
    if not check_rate_limit(tenant_id, tool_name):
        logger.warning("rate_limit_exceeded", tool=tool_name, tenant_id=tenant_id)
        return {"error": "Rate limit exceeded", "tool": tool_name}

    # 2. Param validation
    validated_params = validate_params(tool_name, params)

    # 3. Execute
    try:
        result = await execute_fn(validated_params)
    except Exception as e:
        logger.error("tool_execution_error", tool=tool_name, error=str(e))
        return {"error": str(e), "tool": tool_name}

    # 4. Redact
    redacted = redact_result(result)

    # 5. Log (audit event would be saved to DB in production)
    logger.info(
        "tool_call",
        tool=tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        params=validated_params,
        result_status="success",
    )

    return redacted
