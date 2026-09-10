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
from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot
from app.schemas.transaction_runs import ProgressUpdate, _bounded_json
from app.services import feature_flag_service
from app.services.transaction_ops import state_service
from app.services.transaction_ops.comparison import compare_transactions
from app.services.transaction_ops.header_report import build_header_report
from app.services.transaction_ops.normalization import (
    TransactionMapping,
    _time,
    normalize_framework_order,
    normalize_netsuite_order,
    source_entity_key,
)
from app.services.transaction_ops.order_reconciliation import reconcile_order


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


class SourceScopeError(ValueError):
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
        "not_verified": 0,
        "skipped_after_window": 0,
        "outside_scope": 0,
        "refund_scan_count": 0,
        "refund_after_id": 0,
        "refund_scan_complete": False,
        "phase": "orders",
        "scan_mode": (
            "metabase"
            if (run.config_snapshot.get("mapping_json") or {}).get("metabase_replica")
            else "keyset"
            if run.config_snapshot.get("source_connection_id")
            else "offset"
        ),
    }


def _page_progress(page, progress, params, config=None):
    keyset = progress.get("scan_mode") == "keyset"
    if page.get("page_complete") is not True or page.get("page") != (1 if keyset else progress["page"]):
        raise ScanChangedError("incomplete_source_page")
    total = page.get("total_count")
    if type(total) is not int or total < 0 or not isinstance(page.get("orders"), list):
        raise ScanChangedError("invalid_page_metadata")
    if keyset:
        progress["expected_total"] = progress["scan_count"] + total
    elif progress["expected_total"] is None:
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
            entities = ((config or {}).get("mapping_json") or {}).get("business_entity_subsidiaries") or {}
            if config and entities.get(source_entity_key(order)) != config["subsidiary_id"]:
                progress["outside_scope"] = progress.get("outside_scope", 0) + 1
            else:
                refs.append(order["number"])
        else:
            progress["skipped_after_window"] += 1
    progress["scan_count"] += len(page["orders"])
    next_page = page.get("next_page")
    if next_page is not None and (type(next_page) is not int or next_page != (2 if keyset else progress["page"] + 1)):
        raise ScanChangedError("invalid_next_page")
    if next_page is None:
        if progress["scan_count"] != progress["expected_total"]:
            raise ScanChangedError("source_scan_incomplete")
        progress["scan_complete"] = True
    progress["pending_refs"], progress["next_page"] = refs, next_page


def _replica_page_progress(page, progress, params, config):
    orders = page.get("orders")
    complete, cursor = page.get("scan_complete"), page.get("next_after_id")
    if (
        page.get("page_complete") is not True
        or not isinstance(orders, list)
        or len(orders) > 20
        or type(complete) is not bool
        or (complete and cursor is not None)
        or (not complete and (not orders or type(cursor) is not int))
    ):
        raise ScanChangedError("incomplete_replica_page")
    references = []
    unscoped = []
    basis = params.get("window_basis", "updated_at")
    entities = config["mapping_json"].get("business_entity_subsidiaries") or {}
    for order in orders:
        identifier = order.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)) or not str(identifier).isdigit():
            raise ScanChangedError("invalid_page_identity")
        identifier = int(identifier)
        observed = _time(order.get(basis))
        if identifier <= progress["last_source_id"] or observed is None:
            raise ScanChangedError("source_ordering_changed")
        if not _time(params["window_start"]) <= observed < _time(params["window_end"]):
            raise ScanChangedError("source_window_unproven")
        progress["last_source_id"] = identifier
        if order.get("business_entity") is None:
            references.append(order["number"])
            unscoped.append(order["number"])
        elif entities.get(source_entity_key(order)) == config["subsidiary_id"]:
            references.append(order["number"])
        else:
            progress["outside_scope"] = progress.get("outside_scope", 0) + 1
    if not complete and cursor != progress["last_source_id"]:
        raise ScanChangedError("invalid_replica_cursor")
    progress["scan_count"] += len(orders)
    progress["expected_total"] = progress["scan_count"] if complete else None
    progress["scan_complete"] = complete
    progress["pending_refs"] = references
    progress["unscoped_replica_refs"] = unscoped


def build_report(source_evidence, target_evidence, config, mapping, *, now, refunds=None):
    account = config["netsuite_account_id"].replace("_", "-").lower()
    scope = target_evidence.get("scope") or {}
    if scope.get("account_id") != account or scope.get("subsidiary_id") != config["subsidiary_id"]:
        raise ValueError("target_scope_mismatch")
    try:
        report = _build_detailed_report(source_evidence, target_evidence, config, mapping, now=now)
    except ValueError:
        report = build_header_report(source_evidence, target_evidence, config, mapping, now=now)
    report["balance"] = reconcile_order(source_evidence, target_evidence, config, refunds=refunds)
    return report


def _build_detailed_report(source_evidence, target_evidence, config, mapping, *, now):
    source = normalize_framework_order(
        source_evidence, mapping=mapping, account_id="frame.work", subsidiary_id=config["subsidiary_id"]
    )
    account = config["netsuite_account_id"].replace("_", "-").lower()
    scope = target_evidence.get("scope") or {}
    if scope.get("account_id") != account or scope.get("subsidiary_id") != config["subsidiary_id"]:
        raise ValueError("target_scope_mismatch")
    targets = [
        normalize_netsuite_order(
            item, mapping=mapping, account_id=account, observed_at=target_evidence["observed_at"], source=source
        )
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


def limit_report(report, *, now):
    """Retain a visible incomplete finding when detail exceeds the review bound.

    A single large order must not repeatedly kill its whole scan. Detailed
    snapshots are explicitly marked incomplete and cannot authorize a repair.
    Known transaction-currency header observations remain available.
    """
    try:
        return _bounded_json(report)
    except ValueError:
        source = TransactionSnapshot.model_validate(report["source"])
        targets = [TransactionSnapshot.model_validate(item) for item in report["targets"]]
        lookup = TransactionLookup.model_validate(report["lookup"])
        limits = {
            "code": "evidence_size_limit",
            "source_line_count": len(source.lines),
            "target_line_counts": [len(item.lines) for item in targets],
        }

        def omit_details(snapshot):
            return snapshot.model_copy(
                update={"lines": (), "tax_details": (), "lines_complete": False, "tax_complete": False}
            )

        source = omit_details(source)
        targets = [omit_details(item) for item in targets]
        summary = {
            "order_reference": source.order_reference,
            "source": source.model_dump(mode="json"),
            "targets": [item.model_dump(mode="json") for item in targets],
            "lookup": lookup.model_dump(mode="json"),
            "comparison": compare_transactions(source, targets, lookup, now=now).model_dump(mode="json"),
            "evidence_limits": limits,
            "balance": report.get("balance"),
            "source_provenance": report.get("source_provenance", {}),
            "netsuite_provenance": {
                key: report.get("netsuite_provenance", {}).get(key) for key in ("scope", "observed_at")
            },
        }
        return _bounded_json(summary)


async def run_investigation(
    db,
    tenant_id: UUID,
    run_id: UUID,
    *,
    _state=None,
    _source_reader=None,
    _source_refunds_reader=None,
    _target_refunds_reader=None,
    _refund_page_reader=None,
    _order_mirror=None,
    _destination_page_reader=None,
    _target_reader=None,
    _page_reader=None,
    _guard_reader=None,
    _create_reader=None,
    _celigo_reader=None,
    _enabled=None,
    _clock=None,
):
    from app.services.ingestion.solidus_sync import save_observed_order
    from app.services.transaction_ops import metabase_reader
    from app.services.transaction_ops.celigo_actions import MAX_READ_CALLS, read_celigo_error_evidence
    from app.services.transaction_ops.netsuite_actions import NetSuiteActionError
    from app.services.transaction_ops.netsuite_changes import read_changed_orders
    from app.services.transaction_ops.netsuite_create import CreateInputError, prepare_create_input
    from app.services.transaction_ops.netsuite_reader import read_netsuite_order
    from app.services.transaction_ops.netsuite_refunds import MAX_REFUND_CALLS, read_netsuite_refunds
    from app.services.transaction_ops.netsuite_transport import (
        MAX_GUARD_READ_CALLS,
        read_create_preview,
        read_guard_snapshot,
    )
    from app.services.transaction_ops.planner import PlanningError, plan_proposal
    from app.services.transaction_ops.refund_reader import read_refund_order_page, read_solidus_refunds
    from app.services.transaction_ops.source_reader import read_framework_order, read_framework_orders_page

    state, clock = _state or state_service, _clock or (lambda: datetime.now(timezone.utc))
    source_reader, target_reader = _source_reader or read_framework_order, _target_reader or read_netsuite_order
    page_reader = _page_reader or read_framework_orders_page
    run = await state.get_run(db, tenant_id, run_id)
    from app.services.transaction_ops.settlement import is_settlement

    settlement = is_settlement(run)
    if getattr(run, "origin", None) == "recovery" and not settlement:
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
        source_step_id = UUID(config["source_step_id"]) if config.get("source_step_id") else None
        direct_source = (
            {"source_connection_id": UUID(config["source_connection_id"])} if config.get("source_connection_id") else {}
        )
        mapping = TransactionMapping.model_validate(config["mapping_json"])
        await save()
        while True:
            if not (await state.get_config(db, tenant_id, run.config_id)).enabled:
                return await finish("stall")
            if not progress["pending_refs"]:
                if progress["scan_complete"]:
                    if (
                        (mapping.solidus_refund_step_id or mapping.metabase_replica)
                        and run.params_json.get("window_start")
                        and not progress.get("refund_scan_complete")
                    ):
                        if not await reserve(18 if mapping.metabase_replica else 2):
                            return await finish("budget")
                        refund_scan = (
                            metabase_reader.read_changed_refund_orders
                            if mapping.metabase_replica
                            else _refund_page_reader or read_refund_order_page
                        )
                        refund_page = await bounded_read(
                            refund_scan(
                                db,
                                tenant_id,
                                mapping.metabase_replica or mapping.solidus_refund_step_id,
                                _time(run.params_json["window_start"]),
                                _time(run.params_json["window_end"]),
                                after_id=progress.get("refund_after_id", 0),
                                **({"now": clock()} if mapping.metabase_replica else {}),
                            )
                        )
                        cursor = refund_page.get("next_after_id")
                        rows = refund_page.get("orders")
                        if (
                            refund_page.get("page_complete") is not True
                            or not isinstance(rows, list)
                            or len(rows) > 100
                            or (
                                cursor is not None
                                and (type(cursor) is not int or cursor <= progress.get("refund_after_id", 0))
                            )
                        ):
                            raise ScanChangedError("refund_cursor_unproven")
                        references = []
                        for row in rows:
                            if (
                                mapping.metabase_replica
                                and row.get("business_entity") is not None
                                and mapping.business_entity_subsidiaries.get(source_entity_key(row))
                                != config["subsidiary_id"]
                            ):
                                progress["outside_scope"] = progress.get("outside_scope", 0) + 1
                            else:
                                references.append(row["number"])
                        progress["pending_refs"] = await state.unseen_references(db, tenant_id, run_id, references)
                        progress["refund_scan_count"] = progress.get("refund_scan_count", 0) + len(rows)
                        progress["phase"] = "refunds"
                        progress["refund_scan_complete"] = cursor is None
                        progress["refund_after_id"] = cursor or progress.get("refund_after_id", 0)
                        await save()
                        continue
                    if (
                        mapping.metabase_replica
                        and mapping.reconciliation_policy is not None
                        and run.params_json.get("window_start")
                        and run.params_json.get("window_basis", "updated_at") == "updated_at"
                        and not progress.get("destination_scan_complete")
                    ):
                        if not await reserve(4):
                            return await finish("budget")
                        page = await bounded_read(
                            (_destination_page_reader or read_changed_orders)(
                                db,
                                tenant_id,
                                UUID(config["netsuite_connection_id"]),
                                config["netsuite_account_id"],
                                config["subsidiary_id"],
                                mapping.reference_field,
                                _time(run.params_json["window_start"]),
                                _time(run.params_json["window_end"]),
                                after_id=progress.get("destination_after_id", 0),
                                page_size=20,
                            )
                        )
                        rows, cursor, complete = (
                            page.get("orders"),
                            page.get("next_after_id"),
                            page.get("scan_complete"),
                        )
                        if (
                            page.get("page_complete") is not True
                            or not isinstance(rows, list)
                            or len(rows) > 20
                            or type(complete) is not bool
                            or (complete and cursor is not None)
                            or (not complete and (not rows or type(cursor) is not int))
                        ):
                            raise ScanChangedError("destination_page_incomplete")
                        last = progress.get("destination_after_id", 0)
                        for row in rows:
                            identifier = row.get("id")
                            modified = _time(row.get("updated_at"))
                            if (
                                type(identifier) is not int
                                or identifier <= last
                                or modified is None
                                or not _time(run.params_json["window_start"])
                                <= modified
                                < _time(run.params_json["window_end"])
                            ):
                                raise ScanChangedError("destination_cursor_unproven")
                            last = identifier
                        if not complete and cursor != last:
                            raise ScanChangedError("destination_cursor_unproven")
                        progress["pending_refs"] = await state.unseen_references(
                            db, tenant_id, run_id, list(dict.fromkeys(row["number"] for row in rows))
                        )
                        progress["destination_after_id"] = last
                        progress["destination_scan_count"] = progress.get("destination_scan_count", 0) + len(rows)
                        progress["destination_scan_complete"] = complete
                        progress["phase"] = "destination"
                        await save()
                        continue
                    return await finish("done")
                if mapping.metabase_replica:
                    if not await reserve(6):
                        return await finish("budget")
                    page = await bounded_read(
                        metabase_reader.read_order_page(
                            db,
                            tenant_id,
                            mapping.metabase_replica,
                            _time(run.params_json["window_start"]),
                            _time(run.params_json["window_end"]),
                            after_id=progress["last_source_id"],
                            page_size=20,
                            basis=run.params_json.get("window_basis", "updated_at"),
                            entity_keys=tuple(
                                key
                                for key, subsidiary in mapping.business_entity_subsidiaries.items()
                                if subsidiary == config["subsidiary_id"]
                            ),
                            now=clock(),
                        )
                    )
                    _replica_page_progress(page, progress, run.params_json, config)
                    await save()
                    continue
                keyset = progress.get("scan_mode") == "keyset"
                if progress["next_page"] is not None and not keyset:
                    progress["page"] = progress["next_page"]
                page_options = (
                    {"after_id": progress["last_source_id"], "updated_before": _time(run.params_json["window_end"])}
                    if keyset
                    else {}
                )
                await save()
                if not await reserve(2):
                    return await finish("budget")
                page = await bounded_read(
                    page_reader(
                        db,
                        tenant_id,
                        source_step_id,
                        _time(run.params_json["window_start"]),
                        page=1 if keyset else progress["page"],
                        page_size=20,
                        **direct_source,
                        **page_options,
                    )
                )
                _page_progress(page, progress, run.params_json, config)
                await save()
                continue
            reference = progress["pending_refs"][0]
            if not await reserve(2, 1):
                return await finish("budget")
            source_options = {"include_sync_data": True} if mapping.line_identity_mode == "inventory_units" else {}
            source = await bounded_read(
                source_reader(db, tenant_id, source_step_id, reference, **source_options, **direct_source)
            )
            orders = source.get("orders") or []
            if len(orders) != 1 or orders[0].get("number") != reference:
                raise ValueError("source_reference_mismatch")
            if mapping.business_entity_subsidiaries.get(source_entity_key(orders[0])) != config["subsidiary_id"]:
                if progress.get("phase") == "refunds" or (
                    mapping.metabase_replica
                    and progress.get("phase") != "destination"
                    and reference in progress.get("unscoped_replica_refs", [])
                ):
                    progress["outside_scope"] = progress.get("outside_scope", 0) + 1
                    progress["pending_refs"] = progress["pending_refs"][1:]
                    await save()
                    continue
                if progress.get("phase") != "destination":
                    raise SourceScopeError
            if direct_source:
                await (_order_mirror or save_observed_order)(
                    db, tenant_id, direct_source["source_connection_id"], orders[0], _time(source["read_at"])
                )
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
            report = limit_report(build_report(source, targets, config, mapping, now=clock()), now=clock())
            from app.services.transaction_ops.commercial_credits import (
                read_commercial_credit_for_order,
                source_adjustment_basis,
            )

            if source_adjustment_basis(orders[0]) and report.get("balance", {}).get("status") == "difference":
                # At most 4 invoice reads +12 application reads, plus OAuth maintenance.
                # Reserve before the optional proof, preserving the runner's hard budget.
                if not await reserve(20):
                    await state.record_finding(
                        db, tenant_id, run_id, reference, report, lease_token=token, now=clock(), final=False
                    )
                    return await finish("budget")
                commercial = await bounded_read(
                    read_commercial_credit_for_order(db, tenant_id, config, orders[0], targets, report)
                )
                if commercial:
                    targets["commercial_credit_evidence"] = commercial
                    report["balance"] = reconcile_order(source, targets, config)
            if mapping.solidus_refund_step_id:
                # Preserve known amounts before extra reads. An exhausted refund
                # budget must not discard the already-collected order evidence.
                # Collection checkpoints do not create or resolve exception cases.
                await state.record_finding(
                    db, tenant_id, run_id, reference, report, lease_token=token, now=clock(), final=False
                )
                refunds = {}
                if not await reserve(2):
                    return await finish("budget")
                try:
                    refunds["source"] = await bounded_read(
                        (_source_refunds_reader or read_solidus_refunds)(
                            db, tenant_id, mapping.solidus_refund_step_id, reference
                        )
                    )
                except (state_service.StateError, FeatureRevokedError):
                    raise
                except Exception:
                    refunds["source"] = {"complete": False, "reason": "source_refunds_unavailable"}
                if len(targets.get("orders") or []) == 1 and targets["orders"][0].get("header_complete") is True:
                    if not await reserve(MAX_REFUND_CALLS + 3):
                        return await finish("budget")
                    try:
                        refunds["target"] = await bounded_read(
                            (_target_refunds_reader or read_netsuite_refunds)(
                                db,
                                tenant_id,
                                UUID(config["netsuite_connection_id"]),
                                config["netsuite_account_id"],
                                config["subsidiary_id"],
                                reference,
                                targets,
                                **(
                                    {"adjustment_profile": mapping.refund_adjustments.model_dump(mode="json")}
                                    if mapping.refund_adjustments
                                    else {}
                                ),
                            )
                        )
                    except (state_service.StateError, FeatureRevokedError):
                        raise
                    except Exception:
                        refunds["target"] = {"complete": False, "reason": "target_refunds_unavailable"}
                report["balance"] = reconcile_order(source, targets, config, refunds=refunds)
                report["refund_evidence"] = refunds
                report = limit_report(report, now=clock())
            if report["order_reference"] != reference:
                raise ValueError("source_reference_mismatch")
            action = report["comparison"]["recommended_action"]
            if (
                not settlement
                and mapping.action_mode == "propose_actions"
                and action
                in {
                    "propose_amount_correction",
                    "no_action",
                    "propose_missing_sync",
                }
            ):
                # Preserve detection even when extra action evidence is unavailable
                # or its budget cannot fit. Models never manufacture this proof.
                await state.record_finding(db, tenant_id, run_id, reference, report, lease_token=token, now=clock())
                current_config = await state.get_config(db, tenant_id, run.config_id)
                guard = celigo = creation = None
                try:
                    if action == "propose_amount_correction":
                        if not await reserve(MAX_GUARD_READ_CALLS):
                            return await finish("budget")
                        guard = await bounded_read(
                            (_guard_reader or read_guard_snapshot)(
                                db, tenant_id, current_config, targets["orders"][0]["record_id"]
                            )
                        )
                    elif action == "propose_missing_sync":
                        creation = prepare_create_input(
                            source,
                            current_config.mapping_json,
                            account_id=current_config.netsuite_account_id,
                            subsidiary_id=current_config.subsidiary_id,
                            now=clock(),
                        )
                        if not await reserve(MAX_GUARD_READ_CALLS):
                            return await finish("budget")
                        guard = await bounded_read(
                            (_create_reader or read_create_preview)(
                                db, tenant_id, current_config, creation.payload_json
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
                    request = plan_proposal(
                        report, targets, current_config, now=clock(), guard=guard, celigo=celigo, creation=creation
                    )
                    proposal = await state.propose(db, tenant_id, run_id, request, lease_token=token, now=clock())
                    report = {**report, "automation": {"status": proposal.status, "proposal_id": str(proposal.id)}}
                except (PlanningError, CreateInputError, NetSuiteActionError) as exc:
                    report = {**report, "automation": {"status": "blocked", "code": str(exc)}}
                except (state_service.StateError, FeatureRevokedError):
                    raise
                except Exception:
                    report = {**report, "automation": {"status": "blocked", "code": "action_evidence_unavailable"}}
            finding = await state.record_finding(
                db, tenant_id, run_id, reference, report, lease_token=token, now=clock()
            )
            if settlement and run.params_json.get("approval_message_id"):
                report = finding.report_json
            progress["processed"] += 1
            balance_status = report["balance"]["status"]
            group = (
                "matched"
                if balance_status == "matched"
                else "needs_review"
                if balance_status in {"difference", "ambiguous", "currency_mismatch", "missing_in_netsuite"}
                else "not_verified"
            )
            progress[group] = progress.get(group, 0) + 1
            progress["pending_refs"] = progress["pending_refs"][1:]
            await save()
    except FeatureRevokedError:
        return await finish("stall")
    except SourceScopeError:
        progress["reason"] = "source_subsidiary_unproven"
        await save()
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
