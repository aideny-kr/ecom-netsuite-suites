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

                    published = await asyncio.wait_for(
                        asyncio.to_thread(publish_investigation, tenant_id, run.id, app=celery_app),
                        timeout=_PUBLISH_TIMEOUT,
                    )
                    dispatch_status = "pending_scheduler" if published is False else "queued"
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
                "accounting_resolution_history": resolutions.get("accounting", {}).get("resolutions", []),
                "accounting_resolution_examples": resolutions.get("accounting", {}).get("examples", []),
                "accounting_resolution_usage": resolutions.get("accounting", {}).get("usage"),
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
    from app.services.transaction_ops.case_groups import preparation_members
    from app.services.transaction_ops.state_service import StateError

    context = kwargs.get("context") or {}
    try:
        if "group_id" not in params or set(params) - {"group_id", "review_run_ids", "status", "search"}:
            raise _ToolError("invalid_parameters")
        db, tenant_id, _ = await _authorize(context, create=True)
        members = await preparation_members(db, tenant_id, **params)
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
        if "case_id" not in params or set(params) - {"case_id", "observation_id", "section"}:
            raise _ToolError("invalid_parameters")
        if "section" in params and "observation_id" not in params:
            return {
                "success": False,
                "error": "A saved evidence section requires observation_id from the prior response's audit_id.",
                "reason": "missing_observation_id",
                "recovery": {
                    "saved_read": "Keep case_id and section; add the exact prior audit_id as observation_id. "
                    "This reuses collected evidence without upstream reads.",
                    "fresh_read": "If current evidence is needed, send only case_id. "
                    "That response contains all collected sections and a new audit_id.",
                },
                "financial_writes": 0,
            }
        db, tenant_id, actor = await _authorize(context, create=False)
        case = await case_service.get_case(db, tenant_id, uuid.UUID(str(params["case_id"])))
        if "observation_id" in params:
            from app.services.transaction_ops.group_investigation import read_observation

            db.info.pop("accounting_correction_candidate", None)
            return await read_observation(db, tenant_id, actor.id, case.id, params, context.get("correlation_id"))
        review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
        import json

        from app.models.audit import AuditEvent
        from app.services.audit_service import log_event
        from app.services.transaction_ops.source_reader import SourceReadError
        from app.services.transaction_ops.tax_correction import (
            ACCOUNTING_DETAIL_SOURCE_FIELDS,
            candidate,
            refresh_source,
        )

        prefetched_source, source_error = None, None
        if context.get("group_preparation") is True:
            try:
                prefetched_source = await refresh_source(
                    db, tenant_id, review["scope"], case.order_reference, include_accounting_detail=True
                )
            except SourceReadError as exc:
                source_error = exc
        # Recipe eligibility is not an evidence boundary. In particular, a paid
        # repriced order needs its existing credit/GL evidence before a treatment
        # can be selected. The collector's native call budget remains enforced.
        evidence = json.loads(
            json.dumps(
                await collect_accounting_evidence(db, tenant_id, review, case.latest_report_json),
                default=str,
            )
        )

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
        correction = None
        try:
            if source_error is not None:
                raise source_error
            source = (
                prefetched_source
                if prefetched_source is not None
                else await refresh_source(
                    db, tenant_id, review["scope"], case.order_reference, include_accounting_detail=True
                )
            )
            evidence["source_refresh"] = source
            from app.services.transaction_ops.line_evidence import compare_source_lines, source_revision_delta

            evidence["line_comparison"] = compare_source_lines(source, evidence)
            sections = evidence.get("sections") or {}
            evidence["source_revision_deltas"] = [
                delta
                for document in [sections.get("sales_order") or {}, *(sections.get("posting_documents") or [])]
                if (delta := source_revision_delta(source, evidence, document.get("id"))) is not None
            ]
            # Existing recipes retain their signed source shape and exact
            # revalidation semantics. The full observation remains above.
            source = {k: v for k, v in source.items() if k not in ACCOUNTING_DETAIL_SOURCE_FIELDS}
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
            if correction is None and evidence.get("commercial_credit_resolution"):
                from app.services.transaction_ops.sales_order_alignment import prepare

                try:
                    correction = await prepare(db, tenant_id, case.id, source, review, evidence)
                except (ValueError, KeyError, NetSuiteEvidenceError) as exc:
                    evidence["blockers"].append(f"sales_order_alignment:{exc}")
            if correction is None:
                from app.services.transaction_ops.credit_reallocation import (
                    build_intent,
                    collect_support,
                    solution_summary,
                )

                try:
                    support = await collect_support(db, tenant_id, evidence["source_refresh"], review, evidence)
                    intent = (
                        build_intent(
                            tenant_id,
                            case.id,
                            evidence["source_refresh"],
                            review,
                            evidence,
                            support,
                        )
                        if support
                        else None
                    )
                    if intent:
                        from app.services.transaction_ops.source_line_alignment import build_intent as alignment_intent

                        evidence["resolution_intents"] = [solution_summary(intent)]
                        alignment = alignment_intent(evidence["source_refresh"], evidence, intent)
                        if alignment:
                            evidence["resolution_intents"].append(alignment)
                        evidence["credit_reallocation_support"] = support
                        from app.services.transaction_ops.accounting_preview import for_intent

                        evidence["native_preview_requests"] = [for_intent(intent, review, support["credit"])]
                        if alignment:
                            evidence["native_preview_requests"].append(
                                for_intent(alignment, review, evidence["sections"]["sales_order"])
                            )
                except (ValueError, KeyError, NetSuiteEvidenceError) as exc:
                    evidence["blockers"].append(f"credit_reallocation:{exc}")
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
        from app.services.transaction_ops.resolution_assessment import assess, reference_provenance

        assessment = assess(
            evidence,
            case.latest_report_json,
            review,
            correction,
            references=await reference_provenance(db, tenant_id, case.id),
        )
        evidence["resolution_assessment"] = assessment
        if correction:
            # This object is the same scoped candidate consumed by the confirmation builder.
            # Retain the explanation/provenance with the signed proposal and later audit.
            correction["resolution_assessment"] = assessment
            from app.services.transaction_ops.resolution_plan import proposal_plan

            plan = proposal_plan(correction, case.latest_report_json)
            correction["resolution_plan"] = plan
            evidence["resolution_plan"] = plan
        evidence_event = await log_event(
            db,
            tenant_id,
            category="transaction_ops",
            action="accounting.evidence.observed",
            actor_id=actor.id,
            resource_type="transaction_case",
            resource_id=str(case.id),
            correlation_id=context.get("correlation_id"),
            payload={"evidence": evidence, "correction_candidate": correction},
        )
        evidence["audit_id"] = str(evidence_event.id)
        from app.services.transaction_ops.accounting_evidence import completion_evidence_summary

        return {
            "success": True,
            "case_id": str(case.id),
            "accounting_evidence": evidence,
            "evidence_summary": completion_evidence_summary(evidence),
            "model_context": {
                "version": 1,
                "data": {
                    "case_id": str(case.id),
                    "evidence_summary": completion_evidence_summary(evidence),
                    "record_links": evidence["record_links"],
                    "detail_access": {
                        "case_id": str(case.id),
                        "observation_id": str(evidence_event.id),
                        "sections": ["source", "documents", "applications", "assessment"],
                        "instruction": "Use this evidence tool with case_id, observation_id and section for "
                        "saved detail (no native API calls). Documents include native line/GL evidence. "
                        "Omitted details are not absent or verified. Use a fresh case-only call when "
                        "current changed state is required, not merely to inspect this observation.",
                    },
                },
            },
        }
    except (ValueError, _ToolError, StateError, NetSuiteEvidenceError) as exc:
        return {"success": False, "error": "Accounting case or scoped configuration unavailable.", "reason": str(exc)}
