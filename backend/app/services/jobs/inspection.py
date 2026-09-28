"""Side-effect-free plan inspection. Never an execution/connector-health proof."""

import hashlib
import json
import math

from app.services.jobs.registry import STEP_REGISTRY, PlanInvalid, validate_plan


def plan_fingerprint(schedule, *, use_pending=False):
    document = {
        "schedule_id": str(schedule.id),
        "tenant_id": str(schedule.tenant_id),
        "owner_id": str(schedule.owner_id),
        "plan_version": schedule.plan_version,
        "use_pending": use_pending,
        "plan": schedule.pending_plan_json if use_pending else schedule.plan_json,
        "instruction": schedule.instruction,
        "catch_up": schedule.catch_up,
        "cron": schedule.cron_expression,
        "timezone": schedule.timezone,
        "budget": schedule.budget_json,
        "delivery": schedule.delivery_json,
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inspect_plan(schedule, *, use_pending=False):
    plan = schedule.pending_plan_json if use_pending else schedule.plan_json
    blockers = []
    steps = []
    try:
        validated = validate_plan(plan)
    except (PlanInvalid, TypeError) as exc:
        blockers = exc.errors if isinstance(exc, PlanInvalid) else ["Invalid step type"]
    else:
        for step in validated.steps:
            spec = STEP_REGISTRY[step.type]
            steps.append(
                {
                    "id": step.id,
                    "type": step.type,
                    "label": spec.label,
                    "kind": spec.kind,
                    "params": step.params,
                    "input_schema": spec.params_schema,
                }
            )
            if step.type == "report.compose" and "playbook_key" in step.params:
                from app.services.report.playbooks import PLAYBOOKS, build_playbook_recipe

                key = step.params["playbook_key"]
                if key not in PLAYBOOKS:
                    blockers.append(f"Step {step.id}: unknown report playbook")
                elif step.params.get("mode", "period") == "period":
                    try:
                        build_playbook_recipe(key, step.params["params"])
                    except (ValueError, TypeError, KeyError, AttributeError):
                        blockers.append(f"Step {step.id}: report inputs are incomplete or invalid")
            if step.type == "agent.review_saved_case":
                from pydantic import ValidationError

                from app.services.jobs.agent_step import AgentStepRequest

                if (schedule.budget_json or {}).get("usd") is not None:
                    blockers.append(f"Step {step.id}: agent USD budgets are unsupported; use its token/time limits")
                try:
                    request = AgentStepRequest.model_validate(step.params)
                    if request.principal_id != schedule.owner_id or request.tenant_id != schedule.tenant_id:
                        blockers.append(f"Step {step.id}: agent principal must match workflow owner and company")
                except ValidationError:
                    blockers.append(f"Step {step.id}: invalid agent inputs or limits")
    for key, value in (schedule.budget_json or {}).items():
        if key in {"seconds", "bytes_scanned", "usd"} and value is None:
            continue
        if (
            key not in {"seconds", "bytes_scanned", "usd"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            blockers.append(f"Invalid budget limit: {key}")
    return {
        "plan_hash": plan_fingerprint(schedule, use_pending=use_pending),
        "plan_version": schedule.plan_version + (1 if use_pending else 0),
        "use_pending": use_pending,
        "structurally_valid": not blockers,
        "execution_verified": False,
        "blockers": blockers,
        "steps": steps,
        "notes": [
            "Checks registered steps, inputs and limits without executing work or contacting providers.",
            "Current source access, policy, credentials and output correctness still require runtime checks.",
            "Read steps can create in-app reports and consume provider budget. Drive upload writes externally.",
        ],
    }
