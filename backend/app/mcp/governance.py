import time
import uuid
from collections import defaultdict
from typing import Any, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.metrics import record_call, record_duration, record_rate_limit_rejection
from app.services import audit_service

logger = structlog.get_logger()

# Rate limit tracking: {tenant_id: {tool_name: [timestamps]}}
_rate_limits: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

TOOL_CONFIGS = {
    "health": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 5,
        "rate_limit_per_minute": 60,
        "requires_entitlement": None,
        "allowlisted_params": [],
    },
    "netsuite.suiteql": {
        "default_limit": 100,
        "max_limit": 1000,
        "timeout_seconds": 30,
        "rate_limit_per_minute": 30,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["query", "limit"],
    },
    "netsuite.suiteql_stub": {
        "default_limit": 100,
        "max_limit": 1000,
        "timeout_seconds": 30,
        "rate_limit_per_minute": 30,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["query", "limit"],
    },
    "data.sample_table_read": {
        "default_limit": 100,
        "max_limit": 1000,
        "timeout_seconds": 10,
        "rate_limit_per_minute": 30,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": ["table_name", "limit"],
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
    "netsuite.connectivity": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 15,
        "rate_limit_per_minute": 10,
        "requires_entitlement": "mcp_tools",
        "allowlisted_params": [],
    },
    "workspace.list_files": {
        "default_limit": 200,
        "max_limit": 500,
        "timeout_seconds": 10,
        "rate_limit_per_minute": 60,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["workspace_id", "directory", "recursive", "limit"],
    },
    "workspace.read_file": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 10,
        "rate_limit_per_minute": 120,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["workspace_id", "file_id", "line_start", "line_end"],
    },
    "workspace.search": {
        "default_limit": 20,
        "max_limit": 50,
        "timeout_seconds": 15,
        "rate_limit_per_minute": 30,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["workspace_id", "query", "search_type", "limit"],
    },
    "workspace.propose_patch": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 10,
        "rate_limit_per_minute": 10,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["workspace_id", "file_path", "unified_diff", "title", "rationale"],
    },
    "workspace.apply_patch": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 30,
        "rate_limit_per_minute": 5,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["changeset_id"],
    },
    "workspace.run_validate": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 60,
        "rate_limit_per_minute": 5,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["workspace_id", "changeset_id"],
    },
    "workspace.run_unit_tests": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 120,
        "rate_limit_per_minute": 5,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["workspace_id", "changeset_id"],
    },
    "workspace.run_suiteql_assertions": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 300,
        "rate_limit_per_minute": 5,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["changeset_id", "assertions"],
    },
    "workspace.deploy_sandbox": {
        "default_limit": None,
        "max_limit": None,
        "timeout_seconds": 600,
        "rate_limit_per_minute": 2,
        "requires_entitlement": "workspace",
        "allowlisted_params": ["changeset_id", "override_reason", "require_assertions"],
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
        "params": {
            k: v for k, v in params.items() if k not in {"password", "secret", "token", "api_key", "credentials"}
        },
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
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Governance wrapper: entitlement → rate limit → param validation → execute → redact → audit.
    """
    correlation_id = correlation_id or str(uuid.uuid4())
    start = time.monotonic()

    # 1. Rate limit check
    if not check_rate_limit(tenant_id, tool_name):
        duration_ms = (time.monotonic() - start) * 1000
        logger.warning(
            "mcp.tool_call",
            tool=tool_name,
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            duration_ms=round(duration_ms, 2),
            status="denied",
        )
        record_rate_limit_rejection(tool_name)
        record_call(tool_name, "denied")

        # Audit the rate limit denial
        if db is not None:
            try:
                tenant_uuid = uuid.UUID(tenant_id) if tenant_id else uuid.uuid4()
                actor_uuid = uuid.UUID(actor_id) if actor_id else None
                await audit_service.log_event(
                    db=db,
                    tenant_id=tenant_uuid,
                    category="tool_call",
                    action="tool.rate_limited",
                    actor_id=actor_uuid,
                    actor_type="user",
                    resource_type="mcp_tool",
                    resource_id=tool_name,
                    correlation_id=correlation_id,
                    payload=create_audit_payload(tool_name, params, error="Rate limit exceeded"),
                    status="denied",
                    error_message="Rate limit exceeded",
                )
            except Exception:
                logger.exception("mcp.audit_write_failed", tool=tool_name)

        return {"error": "Rate limit exceeded", "tool": tool_name}

    # 2. Param validation
    validated_params = validate_params(tool_name, params)

    # 2b. Pre-execution audit event
    if db is not None:
        try:
            tenant_uuid = uuid.UUID(tenant_id) if tenant_id else uuid.uuid4()
            actor_uuid = uuid.UUID(actor_id) if actor_id else None
            await audit_service.log_event(
                db=db,
                tenant_id=tenant_uuid,
                category="tool_call",
                action="tool.requested",
                actor_id=actor_uuid,
                actor_type="user",
                resource_type="mcp_tool",
                resource_id=tool_name,
                correlation_id=correlation_id,
                payload={"tool_name": tool_name, "params": validated_params},
                status="pending",
            )
        except Exception:
            logger.exception("mcp.audit_write_failed", tool=tool_name)

    # 3. Execute
    try:
        context = {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "db": db,
            "correlation_id": correlation_id,
        }
        result = await execute_fn(validated_params, context=context)
    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        logger.error(
            "mcp.tool_call",
            tool=tool_name,
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            duration_ms=round(duration_ms, 2),
            status="error",
            error=str(e),
        )
        record_call(tool_name, "error")
        record_duration(tool_name, duration_ms / 1000)

        # Audit the error
        if db is not None:
            try:
                tenant_uuid = uuid.UUID(tenant_id) if tenant_id else uuid.uuid4()
                actor_uuid = uuid.UUID(actor_id) if actor_id else None
                await audit_service.log_event(
                    db=db,
                    tenant_id=tenant_uuid,
                    category="tool_call",
                    action="tool.failed",
                    actor_id=actor_uuid,
                    actor_type="user",
                    resource_type="mcp_tool",
                    resource_id=tool_name,
                    correlation_id=correlation_id,
                    payload=create_audit_payload(tool_name, validated_params, error=str(e)),
                    status="error",
                    error_message=str(e),
                )
            except Exception:
                logger.exception("mcp.audit_write_failed", tool=tool_name)

        return {"error": str(e), "tool": tool_name}

    # 4. Redact
    redacted = redact_result(result)

    # 5. Log + metrics
    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "mcp.tool_call",
        tool=tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        duration_ms=round(duration_ms, 2),
        status="success",
    )
    record_call(tool_name, "success")
    record_duration(tool_name, duration_ms / 1000)

    # 6. Audit to DB
    if db is not None:
        try:
            tenant_uuid = uuid.UUID(tenant_id) if tenant_id else uuid.uuid4()
            actor_uuid = uuid.UUID(actor_id) if actor_id else None
            await audit_service.log_event(
                db=db,
                tenant_id=tenant_uuid,
                category="tool_call",
                action="tool.executed",
                actor_id=actor_uuid,
                actor_type="user",
                resource_type="mcp_tool",
                resource_id=tool_name,
                correlation_id=correlation_id,
                payload=create_audit_payload(tool_name, validated_params, result=result),
                status="success",
            )
        except Exception:
            logger.exception("mcp.audit_write_failed", tool=tool_name)

    return redacted
