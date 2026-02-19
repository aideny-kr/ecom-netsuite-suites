"""Lightweight service for logging NetSuite API request/response exchanges."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.netsuite_api_log import NetSuiteApiLog

logger = structlog.get_logger()

MAX_BODY_SIZE = 10 * 1024  # 10 KB max stored body


def _truncate(text: str | None, max_len: int = MAX_BODY_SIZE) -> str | None:
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... [truncated, {len(text)} total bytes]"


def _sanitize_headers(headers: dict | None) -> dict | None:
    if not headers:
        return None
    sanitized = {}
    for k, v in headers.items():
        lower = k.lower()
        if lower in (
            "authorization",
            "x-api-key",
            "cookie",
            "set-cookie",
            "proxy-authorization",
            "x-netsuite-authorization",
        ):
            sanitized[k] = "***REDACTED***"
        else:
            sanitized[k] = v
    return sanitized


async def log_netsuite_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID | None,
    method: str,
    url: str,
    request_body: str | None = None,
    response_status: int | None = None,
    response_body: str | None = None,
    response_time_ms: int = 0,
    error_message: str | None = None,
    source: str = "unknown",
    request_headers: dict | None = None,
) -> None:
    """Log a NetSuite API request/response. Fire-and-forget — won't fail the caller."""
    try:
        log_entry = NetSuiteApiLog(
            tenant_id=tenant_id,
            connection_id=connection_id,
            direction="outbound",
            method=method.upper(),
            url=url,
            request_headers=_sanitize_headers(request_headers),
            request_body=_truncate(request_body),
            response_status=response_status,
            response_body=_truncate(response_body),
            response_time_ms=response_time_ms,
            error_message=_truncate(error_message, 2000),
            source=source,
        )
        db.add(log_entry)
        # Don't flush — let the caller's commit handle it.
    except Exception:
        logger.warning("netsuite_api_logger.write_failed", exc_info=True)
