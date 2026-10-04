"""Receipts for bounded scheduled order detection, without correction authority.

The existing runner owns provider reads, budgets and cases. This projection
distinguishes observations made at different times from proven contemporaneous
absence. It deliberately defines no company sync SLA or accounting treatment.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.tenant import Tenant
from app.models.user import User


async def authorize(db, tenant_id, run, config):
    from app.services.transaction_ops import state_service as state

    if (
        config.tenant_id != tenant_id
        or not config.enabled
        or not config.schedule_enabled
        or config.config_key != run.config_snapshot.get("config_key")
        or not await db.scalar(select(Tenant.is_active).where(Tenant.id == tenant_id))
    ):
        raise state.StateError("scheduled_detection_access_revoked", 403)
    actor = await db.scalar(select(User).where(User.id == config.created_by, User.tenant_id == tenant_id))
    if actor is None or actor.global_role == "superadmin":
        raise state.StateError("scheduled_detection_access_revoked", 403)
    for permission in ("recon.run", "connections.view"):
        await state._human(db, tenant_id, actor, permission)
    await state._check_bindings(db, tenant_id, config)
    return actor


def classify(report, *, now, scope=None):
    result = {"outcome": "incomplete_evidence", "reason": "identity_or_coverage_unproven", "financial_approval": None}
    try:
        source, lookup, targets = report["source"], report["lookup"], report["targets"]

        def clock(value):
            observed = datetime.fromisoformat(value)
            if observed.utcoffset() is None:
                raise ValueError("naive_observation")
            return observed

        def fresh(value):
            return timedelta(0) <= now - clock(value) <= timedelta(minutes=15)

        if not (
            source["authoritative"] is True
            and lookup["complete"] is True
            and lookup["authoritative"] is True
            and fresh(source["observed_at"])
            and fresh(lookup["observed_at"])
            and source["system"] == lookup["source_system"] == "framework"
            and source["account_id"] == lookup["source_account_id"]
            and source["record_id"] == lookup["source_record_id"]
            and source["order_reference"] == lookup["order_reference"] == report["order_reference"]
            and source["subsidiary_id"] == lookup["target_subsidiary_id"]
            and lookup["target_record_type"] == "salesorder"
            and clock(source["updated_at"]) <= clock(source["observed_at"])
        ):
            return result
        if scope and (
            lookup["target_account_id"] != scope["netsuite_account_id"].replace("_", "-").lower()
            or lookup["target_subsidiary_id"] != scope["subsidiary_id"]
            or lookup["target_record_type"] != scope["record_type"]
        ):
            return result
        for target in targets:
            if not (
                target["system"] == "netsuite"
                and target["account_id"] == lookup["target_account_id"]
                and target["subsidiary_id"] == lookup["target_subsidiary_id"]
                and target["record_type"] == lookup["target_record_type"]
                and target["order_reference"] == source["order_reference"]
                and target["currency"] == source["currency"]
                and target["authoritative"] is True
                and fresh(target["observed_at"])
            ):
                return result
        if clock(lookup["observed_at"]) < clock(source["updated_at"]):
            return {**result, "outcome": "timing_difference", "reason": "destination_observed_before_source_version"}
        if not targets:
            return {**result, "outcome": "observed_missing", "reason": "complete_exact_lookup_empty"}
        if len(targets) != 1:
            return {**result, "outcome": "needs_review", "reason": "multiple_exact_matches"}
        from app.services.transaction_ops.case_service import _cleared

        if _cleared(report, now):
            return {**result, "outcome": "no_discrepancy", "reason": "order_total_tax_refunds_match"}
        if report["balance"]["status"] == "difference":
            return {**result, "outcome": "needs_review", "reason": "observed_amount_difference"}
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return result


async def receipt(db, tenant_id, run, config, report, *, now):
    from app.services.chat.execution_provenance import load_skill_snapshot
    from app.services.transaction_ops import context_provenance
    from app.services.transaction_ops import state_service as state

    # No book/period is inferred from an order timestamp. The manifest records
    # available revisions, not permission to apply a policy to this observation.
    manifest = await context_provenance.context_manifest(db, tenant_id, config, actor_id=config.created_by)
    skill = load_skill_snapshot("accounting_operations")
    return {
        **classify(report, now=now, scope=run.config_snapshot),
        "schema_version": 1,
        "detector": "scheduled_order_evidence_v1",
        "run_id": str(run.id),
        "principal_id": str(config.created_by),
        "rules": {
            "config_id": str(config.id),
            "config_key": run.config_snapshot["config_key"],
            "mapping_sha256": state.business_digest(run.config_snapshot["mapping_json"]),
        },
        "skill": {
            "slug": "accounting_operations",
            "version": skill["version"] if skill else None,
            "applied": False,
            "reason": "deterministic_detection_no_model_synthesis",
        },
        "accounting_context": {
            "version": manifest.get("version"),
            "audit_id": manifest.get("audit_id"),
            "binding_sha256": manifest.get("binding_sha256"),
            "status": manifest.get("status", "scope_required"),
            "policy_applied": False,
            "entries": [
                {key: entry[key] for key in ("key", "revision", "content_sha256", "scope", "status")}
                for entry in manifest.get("entries", [])
            ],
        },
    }
