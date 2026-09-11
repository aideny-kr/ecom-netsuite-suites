"""Investigation-only tools. No tool in this family can approve or execute a repair."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from decimal import Decimal, DecimalException
from types import SimpleNamespace

from sqlalchemy import select

from app.core.dependencies import has_permission
from app.models.tenant import Tenant
from app.models.user import User
from app.services import feature_flag_service
from app.workers.celery_app import celery_app

_PARAMS = {
    "configs": frozenset(),
    "run": frozenset({"config_id", "order_references", "window_start", "window_end"}),
    "status": frozenset({"run_id", "case_id"}),
    "groups": frozenset({"group_id", "limit", "offset", "review_run_ids", "status", "search"}),
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


async def _authorize(context, *, create, fresh=False):
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
    flags = await feature_flag_service.get_all_flags(db, tenant_id) if fresh else None
    for flag in ("celigo", "reconciliation"):
        enabled = (
            flags.get(flag, False) if flags is not None else await feature_flag_service.is_enabled(db, tenant_id, flag)
        )
        if not enabled:
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
        balance = report.get("balance") or {}
        amounts = balance.get("amounts") if isinstance(balance, dict) else None
        if amounts is not None and not isinstance(amounts, dict):
            raise _ToolError("invalid_stored_evidence")
        if amounts:
            target_currency = _text(balance.get("target_currency"))
            if target_currency and target_currency != currency:
                prefix[2] = f"source: {currency}; target: {target_currency}"
            details = [{"field": key, **(amounts.get(key) or {})} for key in ("order_total", "tax", "refunds")]
        else:
            details = differences or [{}]
        for difference in details:
            if not isinstance(difference, dict):
                raise _ToolError("invalid_stored_evidence")
            if len(rows) >= _MAX_ROWS:
                truncated = True
                break
            if differences or amounts:
                values = [_text(difference.get(key)) for key in ("field", "source", "target", "delta")]
                if values[0] is None or any(
                    value is None and (not amounts or difference.get(key) is not None)
                    for key, value in zip(("source", "target", "delta"), values[1:])
                ):
                    raise _ToolError("invalid_stored_evidence")
                try:
                    if any(not Decimal(value).is_finite() for value in values[1:] if value is not None):
                        raise _ToolError("invalid_stored_evidence")
                except DecimalException:
                    raise _ToolError("invalid_stored_evidence") from None
            else:
                values = [None] * 4
            rows.append(prefix + values)
        if truncated:
            break
    return rows, truncated


def _finding_summary(finding):
    from app.services.transaction_ops.accounting_review import metric_assessment

    report = finding.report_json
    balance, comparison = report.get("balance") or {}, report.get("comparison") or {}
    automation = report.get("automation") or {}
    return {
        "order_reference": _text(report.get("order_reference")),
        "case_id": _text(report.get("case_id")),
        "reconciliation_status": _text(balance.get("status")),
        "reason": _text(balance.get("reason")),
        "reconciliation": metric_assessment(report),
        "observed_at": {
            "source": _text(balance.get("source_observed_at") or (report.get("source") or {}).get("observed_at")),
            "target": _text(balance.get("target_observed_at")),
        },
        "missing_metrics": [
            key for key in ("order_total", "tax", "refunds") if key in balance.get("missing_metrics", [])
        ],
        "recommended_action": _text(comparison.get("recommended_action")),
        "resolution_status": _text(automation.get("status")),
        "resolution_blocker": _text(automation.get("code")),
        "repair_findings": [
            _text(item.get("code")) for item in comparison.get("findings", [])[:10] if isinstance(item, dict)
        ],
    }


async def _execute(operation, params, context):
    state = None
    try:
        db, tenant_id, actor = await _authorize(context, create=operation == "run")
        if not isinstance(params, dict) or set(params) - _PARAMS[operation]:
            raise _ToolError("invalid_parameters")
        state, run_request = _state_dependencies()
        if operation == "groups":
            from app.services.transaction_ops.case_groups import group_members, list_groups

            limit, offset = params.get("limit", 20), params.get("offset", 0)
            if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 50 or offset < 0:
                raise _ToolError("invalid_parameters")
            scope = {key: params[key] for key in ("review_run_ids", "status", "search") if key in params}
            if "group_id" in params:
                result = await group_members(db, tenant_id, params["group_id"], limit=limit, offset=offset, **scope)
            else:
                result = await list_groups(db, tenant_id, limit=limit, offset=offset, **scope)
            return {"success": True, **result}
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
                        "source_connection_id": str(config.source_connection_id)
                        if getattr(config, "source_connection_id", None)
                        else None,
                        "netsuite_account_id": _text(getattr(config, "netsuite_account_id", None)),
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
        if set(params) not in ({"run_id"}, {"case_id"}):
            raise _ToolError("invalid_parameters")
        if "case_id" in params:
            from app.services.transaction_ops import case_service
            from app.services.transaction_ops.accounting_review import accounting_context
            from app.services.transaction_ops.resolution_guidance import investigation_guidance
            from app.services.transaction_ops.resolution_history import history

            case_id = uuid.UUID(str(params["case_id"]))
            case = await case_service.get_case(db, tenant_id, case_id)
            observations = await case_service.list_observations(db, tenant_id, case_id, limit=6)
            resolutions = await history(db, tenant_id, case_id, limit=3)
            finding = SimpleNamespace(report_json=case.latest_report_json)
            rows, capped = _finding_rows([finding])
            return {
                "success": True,
                "case_id": str(case.id),
                "status": case.status,
                "last_observed_at": case.last_observed_at.isoformat(),
                "findings": [_finding_summary(finding)],
                "investigation_guidance": investigation_guidance(case.latest_report_json),
                "accounting_review": await accounting_context(
                    db, tenant_id, getattr(case, "scope_json", None) or {}, case.latest_report_json
                ),
                "resolution_history": resolutions["resolutions"],
                "resolution_examples": resolutions["examples"],
                "resolution_usage": resolutions["usage"],
                "resolution_history_url": f"/api/v1/transaction-ops/cases/{case.id}/resolution-history",
                "history": [
                    {
                        "run_id": str(item.run_id),
                        "observed_at": item.observed_at.isoformat(),
                        "reconciliation_status": _text((item.report_json.get("balance") or {}).get("status")),
                    }
                    for item in observations[:5]
                ],
                "columns": ["order_reference", "recommended_action", "currency", "field", "source", "target", "delta"],
                "rows": rows,
                "row_count": len(rows),
                "truncated": capped
                or len(observations) > 5
                or resolutions["truncated"]
                or resolutions["examples_truncated"],
                "query": "",
                "suppress_llm_value": True,
                "source_kind": "transaction_ops",
            }
        run_id = uuid.UUID(str(params.get("run_id")))
        run = await state.get_run(db, tenant_id, run_id)
        findings = await state.list_findings(db, tenant_id, run_id, offset=0, limit=_MAX_FINDINGS)
        more = len(findings) == _MAX_FINDINGS and bool(
            await state.list_findings(db, tenant_id, run_id, offset=_MAX_FINDINGS, limit=1)
        )
        rows, capped = _finding_rows(findings)
        proposals = await state.list_proposals(db, tenant_id, run_id=run_id, limit=51)
        child, blocked = None, None
        if run.termination_reason == "budget":
            from app.services.transaction_ops.continuation import continuation_result

            child, blocked = await continuation_result(db, tenant_id, run_id)
        from app.services.transaction_ops.accounting_review import accounting_context

        return {
            "success": True,
            "run_id": str(run.id),
            "status": run.status,
            "termination_reason": run.termination_reason,
            "settlement": (getattr(run, "progress_json", None) or {}).get("settlement"),
            "continuation_run_id": str(child.id) if child else None,
            "continuation_blocked": blocked,
            "review_url": f"/transaction-operations/runs/{run.id}",
            "findings": [_finding_summary(finding) for finding in findings[:50]],
            "accounting_review": await accounting_context(
                db,
                tenant_id,
                getattr(run, "config_snapshot", None) or {},
                findings[0].report_json if len(findings) == 1 else None,
            ),
            "proposals": [
                {
                    "id": str(proposal.id),
                    "order_reference": proposal.order_reference,
                    "action": proposal.action,
                    "status": proposal.status,
                }
                for proposal in proposals[:50]
            ],
            "columns": ["order_reference", "recommended_action", "currency", "field", "source", "target", "delta"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": bool(more or capped or len(findings) > 50 or len(proposals) > 50),
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


async def execute_groups(params: dict, **kwargs) -> dict:
    return await _with_deadline("groups", params, kwargs.get("context") or {})


async def execute_accounting_group(params: dict, **kwargs) -> dict:
    """Freeze scoped membership for the server-side proposal handoff; no writes."""
    from app.services.transaction_ops.case_groups import group_members
    from app.services.transaction_ops.state_service import StateError

    context = kwargs.get("context") or {}
    try:
        if "group_id" not in params or set(params) - {"group_id", "review_run_ids", "status", "search"}:
            raise _ToolError("invalid_parameters")
        db, tenant_id, _ = await _authorize(context, create=True)
        members = []
        for offset in range(0, 500, 50):
            page = await group_members(db, tenant_id, **params, limit=50, offset=offset)
            members.extend(page["cases"])
            if not page["has_next"]:
                break
        else:
            raise _ToolError("Group exceeds 500 cases; narrow the period or entity. No partial group was prepared.")
        if not members or len({m["case_id"] for m in members}) != len(members):
            raise _ToolError("Group is empty or changed; refresh the exact scoped group.")
        db.info["accounting_group_selection"] = {"group_id": params["group_id"], "scope": params, "members": members}
        return {"success": True, "group_id": params["group_id"], "case_count": len(members), "financial_writes": 0}
    except (ValueError, _ToolError, StateError) as exc:
        return {"success": False, "error": str(exc)}


async def execute_accounting_evidence(params: dict, **kwargs) -> dict:
    """Exact-case native reads; caller cannot choose another account or inject SQL."""
    from app.services.transaction_ops import case_service
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
    from app.services.transaction_ops.state_service import StateError

    context = kwargs.get("context") or {}
    try:
        if set(params) != {"case_id"}:
            raise _ToolError("invalid_parameters")
        db, tenant_id, actor = await _authorize(context, create=False)
        case = await case_service.get_case(db, tenant_id, uuid.UUID(str(params["case_id"])))
        review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
        import json

        evidence = json.loads(
            json.dumps(await collect_accounting_evidence(db, tenant_id, review, case.latest_report_json), default=str)
        )
        from app.models.audit import AuditEvent
        from app.services.audit_service import log_event
        from app.services.transaction_ops.source_reader import SourceReadError
        from app.services.transaction_ops.tax_correction import candidate, refresh_source

        integration = await db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.resource_type == "transaction_case",
                AuditEvent.resource_id == str(case.id),
                AuditEvent.action == "accounting_correction.integration.observed",
                AuditEvent.actor_type == "system",
            )
            .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        if integration:
            evidence["saved_integration_observation"] = {
                "audit_id": str(integration.id),
                "observed_at": integration.timestamp.isoformat(),
                "evidence": integration.payload,
                "freshness": "Stored current-definition observation; not proof of historical execution.",
            }
        db.info.pop("accounting_correction_candidate", None)
        try:
            source = await refresh_source(db, tenant_id, review["scope"], case.order_reference)
            evidence["source_refresh"] = source
            from app.services.transaction_ops.commercial_credits import collect_commercial_credits

            await collect_commercial_credits(db, tenant_id, review, case.latest_report_json, source, evidence)
            correction = candidate(evidence, case.latest_report_json, review, source)
            if correction is None and review.get("sales_credit_profile"):
                from app.services.transaction_ops.sales_credit import build_candidate, collect_support

                try:
                    support = await collect_support(db, tenant_id, source, case.latest_report_json, review, evidence)
                    if support:
                        correction = build_candidate(
                            tenant_id=tenant_id,
                            case_id=case.id,
                            source=source,
                            report=case.latest_report_json,
                            review=review,
                            support=support,
                        )
                        evidence["sales_credit_support"] = support
                except (ValueError, NetSuiteEvidenceError, SourceReadError) as exc:
                    evidence["blockers"].append(f"sales_credit:{exc}")
            if correction:
                evidence["assessment"]["correction_ready"] = "ready_for_exact_human_approval"
                evidence["blockers"] = [
                    b
                    for b in evidence["blockers"]
                    if b != "accounting_treatment_and_supported_posted_adjustment_not_established"
                ]
                correction["case_id"] = str(case.id)
                correction["tenant_id"] = str(tenant_id)
                db.info["accounting_correction_candidate"] = correction
                evidence["correction_candidate"] = {
                    "next_action": "Call this tool to DISPLAY an approval card. Execution requires human approval.",
                    "tool_name": f"ext__{uuid.UUID(correction['connector_id']).hex}__"
                    + ("ns_createRecord" if correction.get("kind") == "sales_adjustment_credit" else "ns_updateRecord"),
                    "params": {
                        "recordType": correction["record_type"],
                        **(
                            {}
                            if correction.get("kind") == "sales_adjustment_credit"
                            else {"recordId": correction["record_id"]}
                        ),
                        "data": json.dumps(correction["proposed_fields"]),
                    },
                    "expected_after": correction["expected_after"],
                    "approval_basis": correction["approval_basis"],
                }
        except SourceReadError as exc:
            evidence["blockers"].append(f"source_refresh:{exc}")
        from app.services.transaction_ops.record_links import evidence_record_links

        evidence = json.loads(json.dumps(evidence, default=str))
        evidence["record_links"] = evidence_record_links(evidence)
        from app.services.transaction_ops.resolution_guidance import investigation_guidance

        evidence["investigation_routes"] = investigation_guidance(case.latest_report_json)["routes"]
        await log_event(
            db,
            tenant_id,
            category="transaction_ops",
            action="accounting.evidence.observed",
            actor_id=actor.id,
            resource_type="transaction_case",
            resource_id=str(case.id),
            correlation_id=context.get("correlation_id"),
            payload={"evidence": evidence},
        )
        return {"success": True, "case_id": str(case.id), "accounting_evidence": evidence}
    except (ValueError, _ToolError, StateError, NetSuiteEvidenceError) as exc:
        return {"success": False, "error": "Accounting case or scoped configuration unavailable.", "reason": str(exc)}
