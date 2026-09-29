"""Saved-state readiness, rechecked at execution. Never a credential-health probe."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select

from app.models.connection import Connection
from app.models.mcp_connector import McpConnector
from app.models.tenant import Tenant
from app.models.user import User
from app.services.jobs.inspection import inspect_plan
from app.services.jobs.registry import STEP_REGISTRY

TEST_SECONDS = 60


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def source_binding(row):
    from app.core.encryption import decrypt_credentials

    try:
        credentials = decrypt_credentials(row.encrypted_credentials)
        # OAuth token renewal preserves the account/client identity. Credential
        # health is intentionally not claimed by this saved-state check.
        credentials = {
            k: v
            for k, v in credentials.items()
            if k not in {"access_token", "refresh_token", "expires_at", "expires_in"}
        }
    except Exception:
        credentials = row.encrypted_credentials
    return {
        "kind": row.__tablename__,
        "id": str(row.id),
        "provider": row.provider,
        "status": row.status,
        "enabled": getattr(row, "is_enabled", True),
        "scope": {
            k: v
            for k, v in (row.metadata_json or {}).items()
            if k in {"account_id", "restlet_url", "project_id", "location", "shared_drive_id"}
        },
        "endpoint": getattr(row, "server_url", None),
        "credential_scope_hash": digest(credentials),
    }


def test_blockers(plan):
    """Only explicitly registered test contracts; read-kind alone proves nothing."""
    blockers = []
    for step in (plan or {}).get("steps", []):
        spec = STEP_REGISTRY.get(step.get("type"))
        contract = spec.test_contract if spec else None
        params = step.get("params", {})
        if contract == "new_period_statement":
            if (
                params.get("playbook_key") not in {"income_statement", "balance_sheet", "trial_balance"}
                or params.get("mode", "period") != "period"
            ):
                blockers.append(
                    "Tests support new period financial statements only; "
                    "refresh, tracking and scan-priced reports require live execution."
                )
        else:
            blockers.append(f"Step {step.get('id')}: {step.get('type')} has no supported test contract.")
    return blockers


async def inspect_readiness(db, schedule, *, use_pending=False):
    from app.services.policy_service import evaluate_tool_call, get_active_policy
    from app.services.transaction_ops.state_service import StateError, _human

    review = inspect_plan(schedule, use_pending=use_pending)
    plan = schedule.pending_plan_json if use_pending else schedule.plan_json
    blockers = []
    permissions = {"schedules.manage"}
    sources = set()
    tools = []
    for step in review["steps"]:
        kind, params = step["type"], step["params"]
        if kind == "report.compose":
            permissions.update({"connections.view", "chat.financial_reports"})
            if "report_id" in params:
                # Existing recipes can carry source-bound arbitrary reads; do not infer readiness.
                blockers.append(
                    "Existing report refresh needs its report-specific source review; build a new playbook report here."
                )
            else:
                from app.services.report.playbooks import build_playbook_recipe

                if params.get("mode", "period") != "period":
                    blockers.append(
                        "Tracking mode resolves a future period at runtime; use a fixed-period plan for this review."
                    )
                else:
                    try:
                        _, recipe = build_playbook_recipe(params["playbook_key"], params["params"])
                        for source in recipe["sources"].values():
                            tools.append((source["tool"], source.get("params", {})))
                            sources.add("bigquery" if source["tool"] == "bigquery_sql" else "netsuite")
                    except (ValueError, KeyError, TypeError):
                        blockers.append("Report inputs cannot be resolved.")
        elif kind == "bigquery_sql":
            permissions.add("connections.view")
            sources.add("bigquery")
            tools.append(("bigquery_sql", params))
        elif kind == "drive.upload":
            permissions.add("connections.view")
            sources.add("google_sheets")
            tools.append(("drive.upload", params))
        elif kind == "recon.run":
            permissions.add("recon.run")
            tools.append(("recon.run", params))
            blockers.append(
                "Reconciliation sources require their own run review; this workflow readiness contract is unavailable."
            )
        elif kind == "agent.review_saved_case":
            permissions.update({"connections.view", "recon.run"})
            tools.append(("transaction_ops_status", {"case_id": params.get("case_id")}))
        elif kind not in {"report.render_pdf", "report.build_xlsx"}:
            blockers.append(f"Step {step['id']}: readiness contract unavailable.")

    owner = await db.scalar(
        select(User)
        .where(User.id == schedule.owner_id, User.tenant_id == schedule.tenant_id)
        .execution_options(populate_existing=True)
    )
    tenant_active = bool(await db.scalar(select(Tenant.is_active).where(Tenant.id == schedule.tenant_id)))
    grants = {}
    for permission in sorted(permissions):
        try:
            await _human(db, schedule.tenant_id, owner, permission)
            grants[permission] = tenant_active
        except StateError:
            grants[permission] = False
        if not grants[permission]:
            blockers.append(f"Workflow owner needs active company access and {permission} permission.")

    policy = await get_active_policy(db, schedule.tenant_id)
    if policy is not None:
        await db.refresh(policy)
    policy_state = (
        None
        if policy is None
        else {
            c.name: getattr(policy, c.name)
            for c in policy.__table__.columns
            if c.name not in {"created_at", "updated_at"}
        }
    )
    for tool, params in tools:
        if not evaluate_tool_call(policy, tool, params)["allowed"]:
            blockers.append(f"Company policy blocks {tool}.")
    if policy is not None and policy.blocked_fields and tools:
        blockers.append(
            "Field-restricted policy cannot be proven for this workflow output; review the policy before execution."
        )

    bindings = []
    # Read only non-secret source identity fields. Do not return labels/account metadata.
    # Bind to the actual registry executors: NetSuite statements use a REST
    # Connection; BigQuery and Drive use MCP connector records.
    for model in (Connection, McpConnector):
        rows = (
            await db.scalars(
                select(model).where(model.tenant_id == schedule.tenant_id).execution_options(populate_existing=True)
            )
        ).all()
        for row in rows:
            provider = row.provider
            if (model is Connection and provider != "netsuite") or (
                model is McpConnector and provider not in {"bigquery", "google_sheets"}
            ):
                continue
            if provider in sources:
                bindings.append(source_binding(row))
    for source in sorted(sources):
        active = [b for b in bindings if b["provider"] == source and b["status"] == "active" and b["enabled"]]
        if not active:
            blockers.append(f"Active {source} source required. Review Connections.")
        elif len(active) > 1:
            blockers.append(f"Multiple active {source} sources cannot be bound unambiguously by this plan.")

    if any(s["type"] == "agent.review_saved_case" for s in review["steps"]) and not blockers:
        from app.services.jobs.agent_step import AgentStepRequest, AgentStoppedError, _preflight
        from app.services.jobs.registry import StepContext

        try:
            await _preflight(
                StepContext(
                    job_id=schedule.id,
                    run_id=uuid.uuid4(),
                    tenant_id=schedule.tenant_id,
                    db=db,
                    actor_id=schedule.owner_id,
                    actor_type="user",
                ),
                AgentStepRequest.model_validate(review["steps"][0]["params"]),
            )
        except (AgentStoppedError, ValueError) as exc:
            blockers.append(f"Saved-evidence context is unavailable: {exc}")
    test_errors = test_blockers(plan) if review["structurally_valid"] else review["blockers"]
    bound = [b for b in bindings if b["status"] == "active" and b["enabled"]]
    signature = digest(
        {
            "owner": str(schedule.owner_id),
            "grants": grants,
            "policy": policy_state,
            "sources": sorted(bound, key=lambda b: b["id"]),
            "blockers": blockers,
        }
    )
    return {
        **review,
        "ready": review["structurally_valid"] and not blockers,
        "readiness_hash": signature,
        "readiness_blockers": blockers,
        "required_permissions": sorted(permissions),
        "sources": sorted(sources),
        "source_bindings": [{k: b[k] for k in ("id", "provider", "status")} for b in bindings],
        "source_binding_hashes": {b["id"]: digest(b) for b in bound},
        "test_supported": not test_errors,
        "test_blockers": test_errors,
        "test_seconds": TEST_SECONDS,
        "notes": [
            "Checks saved source, owner permission and policy state without executing work. "
            "Credentials and source results are checked when a run executes.",
            "Tests create new in-app report previews and may read connected financial data. "
            "They never deliver externally or approve a plan.",
        ],
    }


def execution_fingerprint(schedule):
    # Pending instructions are not the approved program. Settings are executable.
    return digest(
        {
            "plan": schedule.plan_json,
            "version": schedule.plan_version,
            "owner": str(schedule.owner_id),
            "budget": schedule.budget_json,
            "delivery": schedule.delivery_json,
            "cron": schedule.cron_expression,
            "timezone": schedule.timezone,
            "catch_up": schedule.catch_up,
        }
    )


async def approval_blocker(db, schedule):
    parameters = schedule.parameters or {}
    saved = parameters.get("workflow_review")
    if not saved:
        return (
            "Validate and approve this workflow before live execution."
            if parameters.get("workflow_review_required")
            else None
        )
    if saved.get("execution_hash") != execution_fingerprint(schedule):
        return "Plan or settings changed. Validate and refresh approval."
    review = await inspect_readiness(db, schedule)
    if not review["ready"] or saved.get("readiness_hash") != review["readiness_hash"]:
        return "Source, permission or policy changed. Validate and refresh approval."
    return None


async def pause_for_review(db, schedule, reason):
    from datetime import datetime, timezone

    from app.services import audit_service

    schedule.paused_at = datetime.now(timezone.utc)
    schedule.pause_reason = reason
    schedule.last_run_status = "paused"
    await audit_service.log_event(
        db,
        tenant_id=schedule.tenant_id,
        category="jobs",
        action="jobs.paused",
        actor_type="system",
        resource_type="schedule",
        resource_id=str(schedule.id),
        payload={"reason": reason, "owner_id": str(schedule.owner_id) if schedule.owner_id else None},
        status="error",
    )
