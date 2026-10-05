"""Receipts for bounded scheduled order detection, without correction authority.

The existing runner owns provider reads, budgets and cases. This projection
distinguishes observations made at different times from proven contemporaneous
absence. It deliberately defines no company sync SLA or accounting treatment.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.tenant import Tenant
from app.models.user import User


async def authorize_config(db, tenant_id, config):
    from app.services.transaction_ops import state_service as state

    if (
        config.tenant_id != tenant_id
        or not config.enabled
        or not config.schedule_enabled
        or not await db.scalar(select(Tenant.is_active).where(Tenant.id == tenant_id))
    ):
        raise state.StateError("scheduled_detection_access_revoked", 403)
    actor = await db.scalar(
        select(User)
        .where(User.id == config.created_by, User.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if actor is None or actor.global_role == "superadmin":
        raise state.StateError("scheduled_detection_access_revoked", 403)
    for permission in ("recon.run", "connections.view"):
        await state._human(db, tenant_id, actor, permission)
    await state._check_bindings(db, tenant_id, config)
    from app.services.transaction_ops.context_provenance import _binding

    if await _binding(db, tenant_id, config) is None:
        raise state.StateError("scheduled_detection_access_revoked", 403)
    return actor


async def authorize(db, tenant_id, run, config):
    from app.services.transaction_ops import state_service as state

    if config.config_key != run.config_snapshot.get("config_key"):
        raise state.StateError("scheduled_detection_access_revoked", 403)
    return await authorize_config(db, tenant_id, config)


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
                and clock(target["updated_at"]) <= clock(target["observed_at"])
            ):
                return result
        if clock(lookup["observed_at"]) < clock(source["updated_at"]):
            return {**result, "outcome": "timing_difference", "reason": "destination_observed_before_source_version"}
        # Native modification clocks describe different business events. Their
        # ordering cannot prove sync lag or invalidate fresh matching metrics.
        if not targets and source["status"] not in {"confirmed", "fulfilled"}:
            return {**result, "outcome": "needs_review", "reason": "source_lifecycle_requires_review"}
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
    from app.services.transaction_ops.normalization import TransactionMapping

    selection = TransactionMapping.model_validate(run.config_snapshot["mapping_json"]).scheduled_context
    manifest = await context_provenance.context_manifest(
        db, tenant_id, config, actor_id=config.created_by, scope=selection.scope if selection else None, now=now
    )
    if manifest.get("status") == "unavailable":
        raise state.StateError("scheduled_detection_access_revoked", 403)
    skill = load_skill_snapshot("accounting_operations")
    verdict = classify(report, now=now, scope=run.config_snapshot)
    context_status = "scope_required"
    if selection:
        selected = next((entry for entry in manifest.get("entries", []) if entry["key"] == selection.key), None)
        context_status = "selected_context_unavailable"
        if selected:
            context_status = selected["status"]
            if (
                not selected.get("usable_as_policy")
                or selected["revision"] != selection.revision
                or selected["content_sha256"] != selection.content_sha256
            ):
                context_status = "selected_context_requires_review"
            elif report.get("source", {}).get("currency") != selection.scope.currency:
                context_status = "selected_currency_conflict"
            else:
                context_status = "approved_advisory"
        if context_status != "approved_advisory":
            verdict = {**verdict, "outcome": "incomplete_evidence", "reason": context_status}
    # The selected scope is human supplied; non-posting order headers cannot
    # verify a GL book/period or interpret free-form policy as treatment rules.
    return {
        **verdict,
        "observed_balance_status": report.get("balance", {}).get("status"),
        "schema_version": 2,
        "detector": "scheduled_order_evidence_v2",
        "version_semantics": {
            "status": "independent_native_clocks",
            "sync_delay_proven": False,
            "reason": "record_modification_order_does_not_establish_sync_causation",
        },
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
            "status": context_status,
            "selection": selection.model_dump(mode="json") if selection else None,
            "selection_current": context_status == "approved_advisory",
            "native_posting_scope_verified": False,
            "policy_applied": False,
            "entries": [
                {key: entry[key] for key in ("key", "revision", "content_sha256", "scope", "status")}
                for entry in manifest.get("entries", [])
            ],
        },
    }
