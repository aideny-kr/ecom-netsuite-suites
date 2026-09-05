"""Deterministic human-review proposals from complete provider evidence.

No external writes and no approval ingress. Recompute the recommendation and
bind provider versions, exact guard state and exact intent into the approval.
"""

from datetime import timedelta

from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot
from app.schemas.transaction_runs import ProposalCreate
from app.services.transaction_ops.comparison import compare_transactions
from app.services.transaction_ops.netsuite_actions import NetSuiteActionError, prepare_correction
from app.services.transaction_ops.normalization import _time
from app.services.transaction_ops.state_service import business_digest


class PlanningError(ValueError):
    pass


def source_fingerprint(source):
    snapshot = TransactionSnapshot.model_validate(source)
    return business_digest(snapshot.model_dump(mode="python", exclude={"observed_at"}))


def plan_proposal(report, targets, config, *, now, guard=None, celigo=None):
    if not config.enabled or config.mapping_json.get("action_mode", "detect_only") != "propose_actions":
        raise PlanningError("actions_disabled")
    source = TransactionSnapshot.model_validate(report["source"])
    records = [TransactionSnapshot.model_validate(item) for item in report["targets"]]
    lookup = TransactionLookup.model_validate(report["lookup"])
    if (lookup.target_account_id, lookup.target_subsidiary_id, lookup.target_record_type) != (
        config.netsuite_account_id.replace("_", "-").lower(),
        config.subsidiary_id,
        config.record_type,
    ):
        raise PlanningError("configuration_scope_changed")
    if report["comparison"]["recommended_action"] not in {
        "propose_amount_correction",
        "propose_missing_sync",
        "no_action",
        "propose_false_alarm_resolution",
    }:
        raise PlanningError("comparison_requires_review")
    comparison = compare_transactions(source, records, lookup, now=now)
    evidence = {
        "schema_version": 1,
        "report": report,
        "source_version": report["source"]["updated_at"],
        "source_fingerprint": source_fingerprint(report["source"]),
    }
    if comparison.recommended_action == "propose_amount_correction":
        if len(targets["orders"]) != 1 or not guard or guard.get("actions_enabled") is not True:
            raise PlanningError("guard_unavailable")
        if not timedelta(0) <= now - _time(guard["observed_at"]) < timedelta(minutes=15):
            raise PlanningError("guard_stale")
        try:
            prepared = prepare_correction(
                targets["orders"][0],
                source,
                reference_field=config.mapping_json["reference_field"],
                now=now,
                legacy_tax=config.mapping_json.get("netsuite_legacy_tax"),
                account_id=config.netsuite_account_id,
                tax_rounding=config.mapping_json.get("netsuite_tax_rounding"),
            )
        except NetSuiteActionError as exc:
            raise PlanningError(str(exc)) from None
        if prepared.before_json != guard.get("snapshot"):
            raise PlanningError("guard_evidence_changed")
        action, before, after = prepared.action, prepared.before_json, prepared.after_json
        evidence["guard"] = guard
        if config.mapping_json.get("netsuite_legacy_tax"):
            evidence["native_tax_rounding"] = config.mapping_json.get("netsuite_tax_rounding")
    elif comparison.recommended_action == "no_action" and celigo and celigo.get("complete") is True:
        if celigo.get("provider") != "celigo" or celigo.get("order_reference") != source.order_reference:
            raise PlanningError("error_identity_unproven")
        scope, error = celigo["scope"], celigo["error"]
        lookup = TransactionLookup.model_validate(
            {
                **lookup.model_dump(),
                "celigo_error_id": error["error_id"],
                "error_order_reference": celigo["order_reference"],
                "error_observed_at": celigo["observed_at"],
                "error_is_open": True,
                "error_scope": {
                    "connection_id": scope["connection_id"],
                    "flow_id": scope["flow_id"],
                    "step_id": scope["import_id"],
                    "target_account_id": scope["account_id"],
                    "target_subsidiary_id": scope["subsidiary_id"],
                    "target_record_type": scope["record_type"],
                    "operation": {"add": "create", "update": "update", "addupdate": "upsert"}.get(
                        scope["operation"], "unknown"
                    ),
                    "kind": error["kind"],
                },
            }
        )
        comparison = compare_transactions(source, records, lookup, now=now)
        if comparison.recommended_action != "propose_false_alarm_resolution":
            raise PlanningError("resolution_unproven")
        action = "resolve_celigo_error"
        before = {"celigo_error_id": error["error_id"], "celigo_error_state": "open"}
        after = {"celigo_error_id": error["error_id"], "celigo_error_state": "resolved"}
        evidence["celigo"] = celigo
    else:
        raise PlanningError("action_not_ready")
    evidence["comparison_fingerprint"] = comparison.evidence_fingerprint
    fingerprint = business_digest(
        {
            "schema_version": 1,
            "action": action,
            "before": before,
            "after": after,
            "comparison": comparison.evidence_fingerprint,
            "source_provenance": {
                key: value for key, value in report.get("source_provenance", {}).items() if key != "read_at"
            },
            "celigo": celigo.get("fingerprint") if action == "resolve_celigo_error" else None,
            "native_tax_rounding": evidence.get("native_tax_rounding"),
        }
    )
    observations = [source.observed_at, lookup.observed_at, *(record.observed_at for record in records)]
    if action == "correct_amounts":
        observations.append(_time(guard["observed_at"]))
    else:
        observations.append(_time(celigo["observed_at"]))
    return ProposalCreate(
        source_record_id=source.record_id,
        order_reference=source.order_reference,
        target_record_id=records[0].record_id,
        action=action,
        currency=source.currency,
        evidence_fingerprint=fingerprint,
        observed_at=min(observations),
        before_json=before,
        after_json=after,
        evidence_json=evidence,
    )
