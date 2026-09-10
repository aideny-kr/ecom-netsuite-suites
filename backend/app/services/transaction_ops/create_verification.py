"""Independent proof of the explicitly approved pending-order creation policy."""

import json
from datetime import timedelta

from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot
from app.schemas.transaction_runs import _bounded_json
from app.services.transaction_ops.comparison import compare_transactions
from app.services.transaction_ops.netsuite_actions import _number
from app.services.transaction_ops.netsuite_create import validate_create_preview
from app.services.transaction_ops.netsuite_reader import _account
from app.services.transaction_ops.normalization import _time
from app.services.transaction_ops.planner import source_fingerprint
from app.services.transaction_ops.state_service import business_digest


def _persistable(proof, proposal, creation):
    """Bound redundant proof after full verification, without widening the ledger.

    Large observations retain headers, original quantities and integrity-bound
    references to the immutable approved native projection. This is explicitly
    a summary, never a partially populated canonical transaction report.
    """
    if len(json.dumps(proof).encode()) <= 48 * 1024:
        return proof
    report, guard = proof["report"], proof["guard"]
    source, target = report["source"], report["targets"][0]

    def observation(snapshot):
        return {
            **{key: value for key, value in snapshot.items() if key not in {"lines", "tax_details"}},
            "line_count": len(snapshot["lines"]),
            "tax_component_count": len(snapshot["tax_details"]),
        }

    native_record = guard["creation"]["record"]
    native_digest = business_digest(native_record)
    originals = {line["key"]: line for line in source["lines"]}
    compact = {
        "evidence_retention": "summary_and_digests",
        "evidence_fingerprint": proposal.evidence_fingerprint,
        "source_unchanged": True,
        "private_source_unchanged": True,
        "source_fingerprint": creation.source_fingerprint,
        "private_source_fingerprint": creation.private_fingerprint,
        "report_fingerprint": business_digest(report),
        "source_observation": observation(source),
        "target_observation": observation(target),
        "lookup": report["lookup"],
        "guard": {
            "observed_at": guard["observed_at"],
            "creation": {key: value for key, value in guard["creation"].items() if key != "record"},
            "record_fingerprint": native_digest,
            "approved_record_fingerprint": business_digest(proposal.after_json["preview"]["record"]),
        },
        "line_observations": [
            {
                "key": line["key"],
                "source_quantity": originals[line["key"]]["quantity"],
                "native_quantity": line["quantity"],
            }
            for line in target["lines"]
        ],
        "creation_comparison": proof["creation_comparison"],
        "creation_policy": proof["creation_policy"],
    }
    # Check the larger completion/recovery envelope too. Failure cannot grant
    # verification; exact provider/approval bounds keep this summary small.
    _bounded_json(
        {
            "code": "independently_verified",
            "reconciled": True,
            "recovery": {"run_id": "0" * 36, "termination_reason": "done"},
            "verification": compact,
        }
    )
    return compact


def verify_created_outcome(proposal, report, *, guard, creation, now):
    """Keep original observations; compare only proven approved unit equivalents.

    The desired creation state is native pending approval (A), not a fulfilled
    or confirmed order. General anomaly detection remains unchanged. The proof
    retains the original native state and quantities alongside this comparison.
    """
    try:
        evidence = proposal.evidence_json
        if (
            proposal.action != "sync_missing_order"
            or proposal.target_record_id is not None
            or evidence.get("schema_version") != 1
            or not creation
            or not guard
            or len(report["targets"]) != 1
            or source_fingerprint(report["source"]) != source_fingerprint(evidence["report"]["source"])
            or creation.source_fingerprint != source_fingerprint(report["source"])
            or creation.private_fingerprint != evidence["creation"]["private_source_fingerprint"]
            or creation.payload_json != proposal.after_json["input"]
        ):
            return None
        payload = creation.payload_json
        preview = validate_create_preview(payload, proposal.after_json["preview"])
        actual = guard["creation"]
        source = TransactionSnapshot.model_validate(report["source"])
        target = TransactionSnapshot.model_validate(report["targets"][0])
        lookup = TransactionLookup.model_validate(report["lookup"])
        if (
            source.status != "confirmed"
            or target.status != "draft"
            or target.updated_at is None
            or actual["record_id"] != target.record_id
            or actual["work_key"] != proposal.work_key
            or _time(actual["version"]) != target.updated_at
            or not timedelta(0) <= now - _time(guard["observed_at"]) < timedelta(minutes=15)
            or (
                lookup.target_account_id,
                lookup.target_subsidiary_id,
                lookup.target_record_type,
                lookup.order_reference,
            )
            != (_account(proposal.netsuite_account_id), proposal.subsidiary_id, "salesorder", proposal.order_reference)
            or actual["tax_profile"] != {key: payload["tax_profile"][key] for key in ("mode", "tax_code_id")}
            or actual["inventory_mode"] != payload["inventory_mode"]
            or json.dumps(actual["record"], sort_keys=True) != json.dumps(preview["record"], sort_keys=True)
        ):
            return None
        source_lines = {line.key: line for line in source.lines}
        approved_lines = {f"line:{line['source_line_id']}": line for line in payload["lines"]}
        if set(source_lines) != set(approved_lines) or {line.key for line in target.lines} != set(source_lines):
            return None
        equivalents = []
        for line in target.lines:
            approved, original = approved_lines[line.key], source_lines[line.key]
            if (
                line.quantity != _number(approved["quantity"])
                or original.quantity != _number(approved["source_quantity"])
                or line.inventory_unit_ids != tuple(sorted(approved["inventory_unit_ids"]))
                or line.sku != approved["source_sku"]
            ):
                return None
            equivalents.append(line.model_copy(update={"quantity": original.quantity}))
        equivalent = target.model_copy(update={"status": source.status, "lines": tuple(equivalents)})
        comparison = compare_transactions(source, [equivalent], lookup, now=now)
        if comparison.recommended_action != "no_action":
            return None
        proof = {
            "source_unchanged": True,
            "private_source_unchanged": True,
            "report": report,
            "guard": guard,
            "creation_comparison": comparison.model_dump(mode="json"),
            "creation_policy": {
                "native_order_status": "A",
                "quantity_multipliers": {
                    line["source_line_id"]: line["quantity_multiplier"] for line in payload["lines"]
                },
            },
        }
        return _persistable(proof, proposal, creation)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
