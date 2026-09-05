"""Investigation-only tools. No tool in this family can approve or execute a repair."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from decimal import Decimal, DecimalException

from sqlalchemy import select

from app.core.dependencies import has_permission
from app.models.tenant import Tenant
from app.models.user import User
from app.services import feature_flag_service
from app.workers.celery_app import celery_app

_PARAMS = {
    "configs": frozenset(),
    "run": frozenset({"config_id", "order_references", "window_start", "window_end"}),
    "status": frozenset({"run_id"}),
}
_MAX_FINDINGS = 100
_MAX_ROWS = 500
_TOOL_TIMEOUT = 20
_PUBLISH_TIMEOUT = 5


class _ToolError(Exception):
    pass


def _state_dependencies():
    # Lazy imports keep Celery/MCP discovery independent of service import order.
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service

    return state_service, RunCreate


async def _authorize(context, *, create):
    db = context.get("db")
    if db is None:
        raise _ToolError("missing_context")
    try:
        tenant_id = uuid.UUID(str(context.get("tenant_id")))
        actor_id = uuid.UUID(str(context.get("actor_id")))
    except (ValueError, TypeError, AttributeError):
        raise _ToolError("missing_context") from None
    actor = (
        await db.execute(
            select(User)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.id == actor_id,
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.actor_type == "user",
                Tenant.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if actor is None or actor.tenant_id != tenant_id or actor.actor_type != "user":
        raise _ToolError("actor_unavailable")
    for permission in ("connections.view", "recon.run") if create else ("connections.view",):
        if not await has_permission(db, actor_id, permission):
            raise _ToolError("permission_denied")
    for flag in ("celigo", "reconciliation"):
        if not await feature_flag_service.is_enabled(db, tenant_id, flag):
            raise _ToolError("feature_disabled")
    return db, tenant_id, actor


def _text(value, *, max_length=255):
    return value if isinstance(value, str) and len(value) <= max_length else None


def _finding_rows(findings):
    rows = []
    truncated = False
    for finding in findings:
        report = finding.report_json
        if not isinstance(report, dict):
            raise _ToolError("invalid_stored_evidence")
        comparison = report.get("comparison")
        if not isinstance(comparison, dict):
            raise _ToolError("invalid_stored_evidence")
        source = report.get("source") or {}
        currency = comparison.get("currency") or (source.get("currency") if isinstance(source, dict) else None)
        prefix = [_text(report.get("order_reference")), _text(comparison.get("recommended_action")), _text(currency)]
        differences = comparison.get("differences", [])
        if not isinstance(differences, (list, tuple)):
            raise _ToolError("invalid_stored_evidence")
        details = differences or [{}]
        for difference in details:
            if not isinstance(difference, dict):
                raise _ToolError("invalid_stored_evidence")
            if len(rows) >= _MAX_ROWS:
                truncated = True
                break
            if differences:
                values = [_text(difference.get(key)) for key in ("field", "source", "target", "delta")]
                if any(value is None for value in values):
                    raise _ToolError("invalid_stored_evidence")
                try:
                    if any(not Decimal(value).is_finite() for value in values[1:]):
                        raise _ToolError("invalid_stored_evidence")
                except DecimalException:
                    raise _ToolError("invalid_stored_evidence") from None
            else:
                values = [None] * 4
            rows.append(prefix + values)
        if truncated:
            break
    return rows, truncated


async def _execute(operation, params, context):
    state = None
    try:
        db, tenant_id, actor = await _authorize(context, create=operation == "run")
        if not isinstance(params, dict) or set(params) - _PARAMS[operation]:
            raise _ToolError("invalid_parameters")
        state, run_request = _state_dependencies()
        if operation == "configs":
            configs = await state.list_configs(db, tenant_id)
            return {
                "success": True,
                "configs": [
                    {
                        "config_id": str(config.id),
                        "name": config.name,
                        "enabled": config.enabled,
                        "schedule_enabled": config.schedule_enabled,
                        "subsidiary_id": config.subsidiary_id,
                        "record_type": config.record_type,
                    }
                    for config in configs[:100]
                ],
                "truncated": len(configs) > 100,
            }
        if operation == "run":
            config_id = uuid.UUID(str(params.get("config_id")))
            correlation = context.get("correlation_id")
            if not isinstance(correlation, str) or not correlation or len(correlation) > 1000:
                raise _ToolError("missing_request_identity")
            identity = f"chat:{context.get('conversation_id') or ''}:{correlation}"
            request = run_request(
                **{key: value for key, value in params.items() if key != "config_id"},
                origin="chat",
                evaluation_key=hashlib.sha256(identity.encode()).hexdigest(),
            )
            # create_run commits its audit and run before returning, then restores
            # the tenant context. A broker outage must not roll that durable work back.
            run = await state.create_run(db, tenant_id, config_id, request, actor=actor)
            dispatch_status = "already_started"
            if run.status == "pending":
                try:
                    from app.services.transaction_ops.scheduler import publish_investigation

                    await asyncio.wait_for(
                        asyncio.to_thread(publish_investigation, tenant_id, run.id, app=celery_app),
                        timeout=_PUBLISH_TIMEOUT,
                    )
                    dispatch_status = "queued"
                except Exception:
                    dispatch_status = "pending_scheduler"
            return {
                "success": True,
                "run_id": str(run.id),
                "status": run.status,
                "dispatch_status": dispatch_status,
                "review_url": f"/transaction-operations/runs/{run.id}",
            }
        run_id = uuid.UUID(str(params.get("run_id")))
        run = await state.get_run(db, tenant_id, run_id)
        findings = await state.list_findings(db, tenant_id, run_id, offset=0, limit=_MAX_FINDINGS)
        more = len(findings) == _MAX_FINDINGS and bool(
            await state.list_findings(db, tenant_id, run_id, offset=_MAX_FINDINGS, limit=1)
        )
        rows, capped = _finding_rows(findings)
        return {
            "success": True,
            "run_id": str(run.id),
            "status": run.status,
            "termination_reason": run.termination_reason,
            "review_url": f"/transaction-operations/runs/{run.id}",
            "columns": ["order_reference", "recommended_action", "currency", "field", "source", "target", "delta"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": bool(more or capped),
            "query": "",
            "suppress_llm_value": True,
            "source_kind": "transaction_ops",
        }
    except _ToolError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        if state is not None and isinstance(exc, getattr(state, "StateError", _ToolError)):
            return {"success": False, "error": exc.code}
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return {"success": False, "error": "invalid_parameters"}
        # Do not put DB/provider/broker exception strings into chat or audit logs.
        return {"success": False, "error": "transaction_investigation_unavailable"}


async def _with_deadline(operation, params, context):
    try:
        async with asyncio.timeout(_TOOL_TIMEOUT):
            return await _execute(operation, params, context)
    except TimeoutError:
        return {"success": False, "error": "transaction_investigation_timeout"}


async def execute_configs(params: dict, **kwargs) -> dict:
    return await _with_deadline("configs", params, kwargs.get("context") or {})


async def execute_run(params: dict, **kwargs) -> dict:
    return await _with_deadline("run", params, kwargs.get("context") or {})


async def execute_status(params: dict, **kwargs) -> dict:
    return await _with_deadline("status", params, kwargs.get("context") or {})
