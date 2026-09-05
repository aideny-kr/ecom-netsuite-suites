"""Bounded, restartable transaction investigations shared by API/chat/Beat.

This pipeline collects evidence and records findings. It cannot approve or send
customer-data writes. Each provider read is preceded by a committed reservation;
the cursor is committed before reads and after each persisted observation.
"""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.models.tenant import Tenant
from app.schemas.transaction_ops import TransactionLookup
from app.schemas.transaction_runs import ProgressUpdate
from app.services import feature_flag_service
from app.services.transaction_ops import state_service
from app.services.transaction_ops.comparison import compare_transactions
from app.services.transaction_ops.normalization import (
    TransactionMapping,
    _time,
    normalize_framework_order,
    normalize_netsuite_order,
)


async def enabled(db, tenant_id):
    tenant = (
        await db.execute(select(Tenant.id).where(Tenant.id == tenant_id, Tenant.is_active.is_(True)))
    ).scalar_one_or_none()
    flags = await feature_flag_service.get_all_flags(db, tenant_id)
    return tenant is not None and flags.get("celigo") is True and flags.get("reconciliation") is True


class ScanChangedError(ValueError):
    pass


class FeatureRevokedError(ValueError):
    pass


def _initial_progress(run):
    current = dict(run.progress_json or {})
    if current and not current.get("restart_scan"):
        return current
    refs = list(run.params_json.get("order_references") or [])
    return {
        "pending_refs": refs,
        "page": 1,
        "next_page": None,
        "scan_complete": bool(refs),
        "expected_total": None,
        "scan_count": 0,
        "last_source_id": 0,
        "processed": 0,
        "restart_scan": False,
        "matched": 0,
        "needs_review": 0,
        "skipped_after_window": 0,
    }


def _page_progress(page, progress, params):
    if page.get("page_complete") is not True or page.get("page") != progress["page"]:
        raise ScanChangedError("incomplete_source_page")
    total = page.get("total_count")
    if type(total) is not int or total < 0 or not isinstance(page.get("orders"), list):
        raise ScanChangedError("invalid_page_metadata")
    if progress["expected_total"] is None:
        progress["expected_total"] = total
    elif progress["expected_total"] != total:
        raise ScanChangedError("source_population_changed")
    refs = []
    for order in page["orders"]:
        identifier = order.get("id")
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, (str, int))
            or not str(identifier).isdigit()
            or len(str(identifier)) > 30
        ):
            raise ScanChangedError("invalid_page_identity")
        identifier = int(identifier)
        if identifier <= progress["last_source_id"]:
            raise ScanChangedError("source_ordering_changed")
        progress["last_source_id"] = identifier
        updated = _time(order.get("updated_at"))
        if updated is None or updated < _time(params["window_start"]):
            raise ScanChangedError("source_window_unproven")
        if updated <= _time(params["window_end"]):
            refs.append(order["number"])
        else:
            progress["skipped_after_window"] += 1
    progress["scan_count"] += len(page["orders"])
    next_page = page.get("next_page")
    if next_page is not None and (type(next_page) is not int or next_page != progress["page"] + 1):
        raise ScanChangedError("invalid_next_page")
    if next_page is None:
        if progress["scan_count"] != total:
            raise ScanChangedError("source_scan_incomplete")
        progress["scan_complete"] = True
    progress["pending_refs"], progress["next_page"] = refs, next_page


def build_report(source_evidence, target_evidence, config, mapping, *, now):
    source = normalize_framework_order(
        source_evidence, mapping=mapping, account_id="frame.work", subsidiary_id=config["subsidiary_id"]
    )
    account = config["netsuite_account_id"].replace("_", "-").lower()
    scope = target_evidence.get("scope") or {}
    if scope.get("account_id") != account or scope.get("subsidiary_id") != config["subsidiary_id"]:
        raise ValueError("target_scope_mismatch")
    targets = [
        normalize_netsuite_order(item, mapping=mapping, account_id=account, observed_at=target_evidence["observed_at"])
        for item in target_evidence["orders"]
    ]
    lookup = TransactionLookup(
        source_system=source.system,
        source_account_id=source.account_id,
        source_record_id=source.record_id,
        order_reference=source.order_reference,
        target_account_id=account,
        target_subsidiary_id=config["subsidiary_id"],
        target_record_type=config["record_type"],
        complete=(target_evidence.get("lookup") or {}).get("complete") is True,
        authoritative=target_evidence.get("provider") == "netsuite",
        observed_at=_time(target_evidence["observed_at"]),
    )
    result = compare_transactions(source, targets, lookup, now=now)
    return {
        "order_reference": source.order_reference,
        "source": source.model_dump(mode="json"),
        "targets": [item.model_dump(mode="json") for item in targets],
        "lookup": lookup.model_dump(mode="json"),
        "comparison": result.model_dump(mode="json"),
        "source_provenance": {
            key: value
            for key, value in source_evidence.items()
            if key in {"source", "scope", "read_at", "celigo_step_id", "connection_id"}
        },
        "netsuite_provenance": {
            "scope": scope,
            "observed_at": target_evidence["observed_at"],
            "records": [
                {
                    "record_id": item["record_id"],
                    "header": item["header"],
                    "periods": item.get("periods"),
                    "completeness_errors": item.get("completeness_errors", []),
                }
                for item in target_evidence["orders"]
            ],
        },
    }


async def run_investigation(
    db,
    tenant_id: UUID,
    run_id: UUID,
    *,
    _state=None,
    _source_reader=None,
    _target_reader=None,
    _page_reader=None,
    _guard_reader=None,
    _celigo_reader=None,
    _enabled=None,
    _clock=None,
):
    from app.services.transaction_ops.celigo_actions import MAX_READ_CALLS, read_celigo_error_evidence
    from app.services.transaction_ops.netsuite_reader import read_netsuite_order
    from app.services.transaction_ops.netsuite_transport import MAX_GUARD_READ_CALLS, read_guard_snapshot
    from app.services.transaction_ops.planner import PlanningError, plan_proposal
    from app.services.transaction_ops.source_reader import read_framework_order, read_framework_orders_page

    state, clock = _state or state_service, _clock or (lambda: datetime.now(timezone.utc))
    source_reader, target_reader = _source_reader or read_framework_order, _target_reader or read_netsuite_order
    page_reader = _page_reader or read_framework_orders_page
    run = await state.get_run(db, tenant_id, run_id)
    if getattr(run, "origin", None) == "recovery":
        from app.services.transaction_ops.recovery import reconcile_operation_run

        return await reconcile_operation_run(db, tenant_id, run_id, _clock=clock)
    token = await state.claim_run(db, tenant_id, run_id, now=clock())
    if token is None:
        return {"run_id": str(run_id), "status": run.status, "termination_reason": run.termination_reason}
    progress = _initial_progress(run)

    async def save():
        await state.update_progress(
            db, tenant_id, run_id, ProgressUpdate(progress_json=progress), lease_token=token, now=clock()
        )

    async def finish(reason):
        await state.finish_run(db, tenant_id, run_id, reason, lease_token=token, now=clock())
        return {
            "run_id": str(run_id),
            "status": "finished",
            "termination_reason": reason,
            "processed": progress["processed"],
            "matched": progress["matched"],
            "needs_review": progress["needs_review"],
        }

    async def reserve(calls, orders=0):
        if not await (_enabled or enabled)(db, tenant_id):
            raise FeatureRevokedError
        return await state.reserve_budget(
            db, tenant_id, run_id, lease_token=token, api_calls=calls, orders=orders, now=clock()
        )

    async def bounded_read(call):
        remaining = (run.deadline_at - clock()).total_seconds()
        if remaining <= 0:
            call.close()
            raise TimeoutError
        async with asyncio.timeout(min(remaining, 170)):
            return await call

    try:
        if not await (_enabled or enabled)(db, tenant_id):
            return await finish("stall")
        config = run.config_snapshot
        mapping = TransactionMapping.model_validate(config["mapping_json"])
        await save()
        while True:
            if not (await state.get_config(db, tenant_id, run.config_id)).enabled:
                return await finish("stall")
            if not progress["pending_refs"]:
                if progress["scan_complete"]:
                    return await finish("done")
                if progress["next_page"] is not None:
                    progress["page"] = progress["next_page"]
                await save()
                if not await reserve(2):
                    return await finish("budget")
                page = await bounded_read(
                    page_reader(
                        db,
                        tenant_id,
                        UUID(config["source_step_id"]),
                        _time(run.params_json["window_start"]),
                        page=progress["page"],
                        page_size=20,
                    )
                )
                _page_progress(page, progress, run.params_json)
                await save()
                continue
            reference = progress["pending_refs"][0]
            if not await reserve(2, 1):
                return await finish("budget")
            source = await bounded_read(source_reader(db, tenant_id, UUID(config["source_step_id"]), reference))
            if not await reserve(10):  # At most7 data reads plus ordinary OAuth token maintenance.
                return await finish("budget")
            targets = await bounded_read(
                target_reader(
                    db,
                    tenant_id,
                    UUID(config["netsuite_connection_id"]),
                    config["netsuite_account_id"],
                    config["subsidiary_id"],
                    reference,
                    mapping.reference_field,
                )
            )
            report = build_report(source, targets, config, mapping, now=clock())
            if report["order_reference"] != reference:
                raise ValueError("source_reference_mismatch")
            action = report["comparison"]["recommended_action"]
            if mapping.action_mode == "propose_actions" and action in {
                "propose_amount_correction",
                "no_action",
                "propose_missing_sync",
            }:
                # Preserve detection even when extra action evidence is unavailable
                # or its budget cannot fit. Models never manufacture this proof.
                await state.record_finding(db, tenant_id, run_id, reference, report, lease_token=token, now=clock())
                current_config = await state.get_config(db, tenant_id, run.config_id)
                guard = celigo = None
                try:
                    if action == "propose_amount_correction":
                        if not await reserve(MAX_GUARD_READ_CALLS):
                            return await finish("budget")
                        guard = await bounded_read(
                            (_guard_reader or read_guard_snapshot)(
                                db, tenant_id, current_config, targets["orders"][0]["record_id"]
                            )
                        )
                    elif action == "no_action" and current_config.target_step_id:
                        if not await reserve(MAX_READ_CALLS):
                            return await finish("budget")
                        celigo = await bounded_read(
                            (_celigo_reader or read_celigo_error_evidence)(
                                db, tenant_id, current_config.target_step_id, reference
                            )
                        )
                    request = plan_proposal(report, targets, current_config, now=clock(), guard=guard, celigo=celigo)
                    proposal = await state.propose(db, tenant_id, run_id, request, lease_token=token, now=clock())
                    report = {**report, "automation": {"status": proposal.status, "proposal_id": str(proposal.id)}}
                except PlanningError as exc:
                    report = {**report, "automation": {"status": "blocked", "code": str(exc)}}
                except (state_service.StateError, FeatureRevokedError):
                    raise
                except Exception:
                    report = {**report, "automation": {"status": "blocked", "code": "action_evidence_unavailable"}}
            await state.record_finding(db, tenant_id, run_id, reference, report, lease_token=token, now=clock())
            progress["processed"] += 1
            action = report["comparison"]["recommended_action"]
            progress["matched" if action == "no_action" else "needs_review"] += 1
            progress["pending_refs"] = progress["pending_refs"][1:]
            await save()
    except FeatureRevokedError:
        return await finish("stall")
    except ScanChangedError:
        progress["restart_scan"] = True
        await save()
        return await finish("stall")
    except TimeoutError:
        return await finish("budget" if clock() >= run.deadline_at else "error")
    except state_service.StateError as exc:
        if exc.code == "run_lease_lost":
            return {"run_id": str(run_id), "status": "yielded", "termination_reason": "stall"}
        raise
    except Exception:
        # Provider helpers use safe error codes, but unexpected library/DB
        # exceptions may carry SQL or bodies. Never persist/return their text.
        return await finish("error")
