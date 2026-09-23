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
from app.services.transaction_ops.read_recovery import ReadBudgetExhaustedError, read_with_recovery
from app.services.transaction_ops.source_eligibility import exclusion_report, payment_failed


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
    refs = list(run.params_json.get("order_references") or [])
    defaults = {
        "pending_refs": refs,
        "page": 1,
        "next_page": None,
        "scan_complete": bool(refs),
        "expected_total": None,
        "scan_count": 0,
        "last_source_id": 0,
        "processed": 0,
        "excluded": 0,
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

    if current.get("restart_scan"):
        # A failed scan starts with fresh cursors. Its reporting-cycle identity
        # still prevents a continuation from opening an extra daily budget.
        if current.get("schedule_cycle_key"):
            defaults["schedule_cycle_key"] = current["schedule_cycle_key"]
        return defaults
    # New schedules carry metadata before the first provider page. Metadata is
    # not a populated checkpoint; fill canonical counters/cursors as well.
    return defaults | current


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
    progress["pending_source_versions"] = {
        order["number"]: order["updated_at"] for order in page["orders"] if order["number"] in refs
    }


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
    progress["pending_source_versions"] = {
        order["number"]: order["updated_at"] for order in orders if order["number"] in references
    }


def build_report(source_evidence, target_evidence, config, mapping, *, now, refunds=None):
    if len(source_evidence.get("orders") or []) == 1 and payment_failed(source_evidence["orders"][0]):
        return exclusion_report(source_evidence)
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
        from app.services.transaction_ops.dependency_index import compact_dependency_evidence

        dependencies = compact_dependency_evidence(report)
        if dependencies:
            # Identity inventory is separate from omitted financial proof and
            # cannot turn this incomplete finding into an actionable comparison.
            summary["refund_dependency_evidence"] = dependencies
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
    _dependency_page_reader=None,
    _dependency_owner_reader=None,
    _dependency_index=None,
    _dependency_seed=None,
    _target_reader=None,
    _page_reader=None,
    _guard_reader=None,
    _create_reader=None,
    _celigo_reader=None,
    _enabled=None,
    _clock=None,
):
    from app.services.ingestion.solidus_sync import save_observed_order
    from app.services.transaction_ops import dependency_index, dependency_scan, metabase_reader, source_snapshot
    from app.services.transaction_ops.call_meter import metered
    from app.services.transaction_ops.celigo_actions import MAX_READ_CALLS, read_celigo_error_evidence
    from app.services.transaction_ops.netsuite_actions import NetSuiteActionError
    from app.services.transaction_ops.netsuite_change_owners import MAX_OWNER_CALLS, read_order_candidates
    from app.services.transaction_ops.netsuite_create import CreateInputError, prepare_create_input
    from app.services.transaction_ops.netsuite_dependency_changes import read_change_page
    from app.services.transaction_ops.netsuite_reader import MAX_API_CALLS as NETSUITE_READ_CALLS
    from app.services.transaction_ops.netsuite_reader import read_netsuite_order
    from app.services.transaction_ops.netsuite_refunds import MAX_REFUND_CALLS, read_netsuite_refunds
    from app.services.transaction_ops.netsuite_transport import (
        MAX_GUARD_READ_CALLS,
        read_create_preview,
        read_guard_snapshot,
    )
    from app.services.transaction_ops.planner import PlanningError, plan_proposal
    from app.services.transaction_ops.read_batch import ReferenceReads, reference_read_batch
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
    # Production claims use PostgreSQL's clock, matching its immutable budget
    # trigger. The host clock can otherwise reject a valid first claim by drift.
    token = await state.claim_run(db, tenant_id, run_id, now=clock() if _clock is not None else None)
    if token is None:
        return {"run_id": str(run_id), "status": run.status, "termination_reason": run.termination_reason}
    progress = _initial_progress(run)
    reference_reads = ReferenceReads()
    previous_reference_hits = progress.get("reference_cache_hits", 0)

    async def save():
        progress["reference_cache_hits"] = previous_reference_hits + reference_reads.hits
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

    async def reserve(calls, orders=0, *, hold=False):
        if not await (_enabled or enabled)(db, tenant_id):
            raise FeatureRevokedError
        return await state.reserve_budget(
            db, tenant_id, run_id, lease_token=token, api_calls=calls, orders=orders, hold=hold, now=clock()
        )

    async def bounded_read(stage, factory, *, retry_calls=0, reserve_retry=None):
        return await read_with_recovery(
            factory,
            stage=stage,
            retry_calls=retry_calls,
            progress=progress,
            reserve=reserve_retry or reserve,
            save=save,
            remaining=lambda: (run.deadline_at - clock()).total_seconds(),
        )

    async def metered_read(stage, factory, *, held, data_calls, **options):
        """A bounded read charged for what it sent rather than the worst case reserved.

        ``held`` is the reservation made with ``hold=True`` just before this read, and
        ``data_calls`` its data share. Only that share can come back: the rest covers
        sign-in token maintenance, which happens when a reader is built and outside the
        metered send path, so it is charged whether or not a refresh occurred. A retry
        pays its own full reservation inside ``bounded_read``, so the hold is settled on
        the first attempt's sends alone: counting the retry's sends against it as well
        would charge them twice. The settle runs on failure too, since a failed read still
        did not make the calls it did not make; if it never runs, finishing the run charges
        the whole hold.
        """
        first_attempt = None

        async def reserve_retry(calls):
            nonlocal first_attempt
            if first_attempt is None:
                first_attempt = meter.calls
            return await reserve(calls)

        async def settle(meter):
            progress["metered_calls"] = progress.get("metered_calls", 0) + meter.calls
            sent = meter.calls if first_attempt is None else first_attempt
            unused = max(0, data_calls - sent)
            await state.settle_budget(
                db, tenant_id, run_id, lease_token=token, release=held, spent=held - unused, now=clock()
            )

        with metered() as meter, reference_read_batch(reference_reads):
            try:
                result = await bounded_read(stage, factory, reserve_retry=reserve_retry, **options)
            except asyncio.CancelledError:
                # Cancelled from outside: no more awaits here. Finishing charges the hold.
                raise
            except BaseException as error:
                # The read's own error must reach the caller: some callers degrade on a
                # failed read, and a bookkeeping error raised here would turn that into an
                # abort. Nothing is lost if the settle fails, since finishing the run
                # charges the hold in full. A read that raises also ends the run without
                # another checkpoint, so the count is saved here or lost with it.
                try:
                    await settle(meter)
                    await save()
                except Exception as bookkeeping:
                    error.add_note(f"settling the read's budget also failed: {bookkeeping!r}")
                raise
            # A successful read is settled outside any handler, so a lost lease here stops
            # the run as it would at the next checkpoint. The count is saved at that
            # checkpoint rather than with another write per read.
            await settle(meter)
            return result

    try:
        if not await (_enabled or enabled)(db, tenant_id):
            return await finish("stall")
        config = run.config_snapshot
        source_step_id = UUID(config["source_step_id"]) if config.get("source_step_id") else None
        direct_source = (
            {"source_connection_id": UUID(config["source_connection_id"])} if config.get("source_connection_id") else {}
        )
        mapping = TransactionMapping.model_validate(config["mapping_json"])
        snapshot_floor = source_snapshot.scan_floor(run, clock()) if direct_source and not settlement else None
        if mapping.line_identity_mode == "inventory_units":
            snapshot_floor = None  # Create-input projections are deliberately not shared.
        if not (await state.get_config(db, tenant_id, run.config_id)).enabled:
            return await finish("stall")
        if run.params_json.get("review"):
            from app.schemas.transaction_runs import ReviewSpan
            from app.services.transaction_ops.daily_evidence import (
                completed_daily_windows,
                completed_observation_windows,
                covered_until,
            )

            span = ReviewSpan.model_validate(run.params_json["review"])
            saved = await completed_observation_windows(db, run, span)
            start, end = (_time(run.params_json[k]) for k in ("window_start", "window_end"))
            if covered_until(start, end, saved) == end:
                daily = await completed_daily_windows(db, run, span)
                progress.update(
                    scan_complete=True,
                    refund_scan_complete=True,
                    destination_scan_complete=True,
                    pending_refs=[],
                    reused_daily_run_ids=[row[2] for row in daily],
                    reused_observation_run_ids=[row[2] for row in saved],
                    review_coverage_complete=covered_until(span.start, span.end, saved) == span.end,
                )
                await save()
                return await finish("done")
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
                            "refund_page",
                            lambda: refund_scan(
                                db,
                                tenant_id,
                                mapping.metabase_replica or mapping.solidus_refund_step_id,
                                _time(run.params_json["window_start"]),
                                _time(run.params_json["window_end"]),
                                after_id=progress.get("refund_after_id", 0),
                                **({"now": clock()} if mapping.metabase_replica else {}),
                            ),
                            retry_calls=18 if mapping.metabase_replica else 2,
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
                        and (
                            not progress.get("dependency_scan_complete")
                            or not progress.get("dependency_index_seed", {}).get("complete")
                        )
                    ):
                        if not progress.get("dependency_index_seed", {}).get("complete"):
                            from app.services.transaction_ops import dependency_seed

                            if not progress.get("dependency_index_seed"):
                                # An older unseeded checkpoint cannot retain a
                                # deletion page already consumed without owners.
                                progress.pop("dependency_scan", None)
                                progress["dependency_scan_complete"] = False
                            if not await (_enabled or enabled)(db, tenant_id):
                                return await finish("stall")
                            # Local indexing spends no provider budget. A leased
                            # checkpoint enforces ownership/deadline before it.
                            await save()
                            progress["dependency_index_seed"] = await (_dependency_seed or dependency_seed.advance)(
                                db,
                                tenant_id,
                                run.config_id,
                                progress.get("dependency_index_seed", {}),
                            )
                            progress["dependency_step_count"] = progress.get("dependency_step_count", 0) + 1
                            await save()
                            continue

                        async def dependency_page(stream, after):
                            if not await reserve(2, hold=True):
                                raise ReadBudgetExhaustedError
                            return await metered_read(
                                "dependency_page",
                                lambda: (_dependency_page_reader or read_change_page)(
                                    db,
                                    tenant_id,
                                    UUID(config["netsuite_connection_id"]),
                                    config["netsuite_account_id"],
                                    config["subsidiary_id"],
                                    mapping.reference_field,
                                    stream,
                                    _time(run.params_json["window_start"]),
                                    _time(run.params_json["window_end"]),
                                    after=after,
                                    page_size=20,
                                ),
                                held=2,
                                data_calls=1,
                                retry_calls=2,
                            )

                        async def dependency_owners(**options):
                            calls = MAX_OWNER_CALLS + 1
                            if not await reserve(calls, hold=True):
                                raise ReadBudgetExhaustedError
                            return await metered_read(
                                "dependency_owners",
                                lambda: (_dependency_owner_reader or read_order_candidates)(
                                    db,
                                    tenant_id,
                                    UUID(config["netsuite_connection_id"]),
                                    config["netsuite_account_id"],
                                    config["subsidiary_id"],
                                    mapping.reference_field,
                                    **options,
                                ),
                                held=calls,
                                data_calls=MAX_OWNER_CALLS,
                                retry_calls=calls,
                            )

                        async def indexed_owners(keys, **options):
                            return await (_dependency_index or dependency_index.affected_order_references)(
                                db,
                                tenant_id,
                                run.config_id,
                                keys,
                                **options,
                            )

                        async def unobserved(refs, **options):
                            return await state.unseen_references(db, tenant_id, run_id, refs, **options)

                        await dependency_scan.advance(
                            progress,
                            read_page=dependency_page,
                            read_owners=dependency_owners,
                            indexed_owners=indexed_owners,
                            unobserved=unobserved,
                        )
                        progress["dependency_step_count"] = progress.get("dependency_step_count", 0) + 1
                        await save()
                        continue
                    return await finish("done")
                if mapping.metabase_replica:
                    if not await reserve(6):
                        return await finish("budget")
                    page = await bounded_read(
                        "source_page",
                        lambda: metabase_reader.read_order_page(
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
                        ),
                        retry_calls=6,
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
                    "source_page",
                    lambda: page_reader(
                        db,
                        tenant_id,
                        source_step_id,
                        _time(run.params_json["window_start"]),
                        page=1 if keyset else progress["page"],
                        page_size=20,
                        **direct_source,
                        **page_options,
                    ),
                    retry_calls=2,
                )
                _page_progress(page, progress, run.params_json, config)
                await save()
                continue
            reference = progress["pending_refs"][0]
            source_options = {"include_sync_data": True} if mapping.line_identity_mode == "inventory_units" else {}
            can_reuse = snapshot_floor is not None and progress.get("phase") == "orders"
            source = None
            if can_reuse:
                source = await source_snapshot.load(
                    db,
                    tenant_id,
                    direct_source["source_connection_id"],
                    reference,
                    since=snapshot_floor,
                    now=clock(),
                    minimum_version=_time(progress.get("pending_source_versions", {}).get(reference)),
                )
            source_reused = source is not None
            if not await reserve(0 if source_reused else 2, 1):
                return await finish("budget")
            if source_reused:
                progress["source_snapshot_hits"] = progress.get("source_snapshot_hits", 0) + 1
            else:
                source = await bounded_read(
                    "source_order",
                    lambda: source_reader(db, tenant_id, source_step_id, reference, **source_options, **direct_source),
                    retry_calls=2,
                )
                progress["source_detail_reads"] = progress.get("source_detail_reads", 0) + 1
                if can_reuse:
                    await source_snapshot.save(
                        db, tenant_id, direct_source["source_connection_id"], reference, source, now=clock()
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
                    db,
                    tenant_id,
                    direct_source["source_connection_id"],
                    orders[0],
                    _time(source["read_at"]),
                    **({"reused": True} if source_reused else {}),
                )
            if payment_failed(orders[0]):
                await state.record_finding(
                    db, tenant_id, run_id, reference, exclusion_report(source), lease_token=token, now=clock()
                )
                progress["excluded"] += 1
                progress["pending_refs"] = progress["pending_refs"][1:]
                await save()
                continue
            if not await reserve(10, hold=True):  # NETSUITE_READ_CALLS data reads plus OAuth maintenance.
                return await finish("budget")
            targets = await metered_read(
                "netsuite_order",
                lambda: target_reader(
                    db,
                    tenant_id,
                    UUID(config["netsuite_connection_id"]),
                    config["netsuite_account_id"],
                    config["subsidiary_id"],
                    reference,
                    mapping.reference_field,
                ),
                held=10,
                data_calls=NETSUITE_READ_CALLS,
                retry_calls=10,
            )
            report = limit_report(build_report(source, targets, config, mapping, now=clock()), now=clock())
            from app.services.transaction_ops.commercial_credits import (
                MAX_CREDIT_READS,
                MAX_INVOICE_READS,
                read_commercial_credit_for_order,
                source_adjustment_basis,
            )

            if source_adjustment_basis(orders[0]) and report.get("balance", {}).get("status") == "difference":
                # MAX_INVOICE_READS + MAX_CREDIT_READS data reads, plus OAuth maintenance.
                # Reserve before the optional proof, preserving the runner's hard budget.
                if not await reserve(20, hold=True):
                    await state.record_finding(
                        db, tenant_id, run_id, reference, report, lease_token=token, now=clock(), final=False
                    )
                    return await finish("budget")
                commercial = await metered_read(
                    "commercial_credit",
                    lambda: read_commercial_credit_for_order(db, tenant_id, config, orders[0], targets, report),
                    held=20,
                    data_calls=MAX_INVOICE_READS + MAX_CREDIT_READS,
                    retry_calls=20,
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
                        "source_refunds",
                        lambda: (_source_refunds_reader or read_solidus_refunds)(
                            db, tenant_id, mapping.solidus_refund_step_id, reference
                        ),
                        retry_calls=2,
                    )
                except (state_service.StateError, FeatureRevokedError, ReadBudgetExhaustedError, TimeoutError):
                    raise
                except Exception:
                    refunds["source"] = {"complete": False, "reason": "source_refunds_unavailable"}
                if len(targets.get("orders") or []) == 1 and targets["orders"][0].get("header_complete") is True:
                    if not await reserve(MAX_REFUND_CALLS + 3, hold=True):
                        return await finish("budget")
                    try:
                        refunds["target"] = await metered_read(
                            "netsuite_refunds",
                            lambda: (_target_refunds_reader or read_netsuite_refunds)(
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
                            ),
                            held=MAX_REFUND_CALLS + 3,
                            data_calls=MAX_REFUND_CALLS,
                            retry_calls=MAX_REFUND_CALLS + 3,
                        )
                    except (state_service.StateError, FeatureRevokedError, ReadBudgetExhaustedError, TimeoutError):
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
                and not source_reused
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
                            "create_preview",
                            lambda: (_guard_reader or read_guard_snapshot)(
                                db, tenant_id, current_config, targets["orders"][0]["record_id"]
                            ),
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
                            "guard_snapshot",
                            lambda: (_create_reader or read_create_preview)(
                                db, tenant_id, current_config, creation.payload_json
                            ),
                        )
                    elif action == "no_action" and current_config.target_step_id:
                        if not await reserve(MAX_READ_CALLS):
                            return await finish("budget")
                        celigo = await bounded_read(
                            "celigo_error",
                            lambda: (_celigo_reader or read_celigo_error_evidence)(
                                db, tenant_id, current_config.target_step_id, reference
                            ),
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
    except ReadBudgetExhaustedError:
        return await finish("budget")
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
            if clock() >= run.deadline_at:
                try:
                    # A progress write can meet the deadline before the next
                    # budget reservation. Keep this row lock through finish:
                    # its idempotent terminal-row path does not check ownership.
                    current = await state.get_run(db, tenant_id, run_id, lock=True)
                    if current.status == "running" and current.lease_token == token:
                        return await finish("budget")
                except state_service.StateError as finish_exc:
                    if finish_exc.code != "run_lease_lost":
                        raise
            return {"run_id": str(run_id), "status": "yielded", "termination_reason": "stall"}
        raise
    except Exception:
        # Provider helpers use safe error codes, but unexpected library/DB
        # exceptions may carry SQL or bodies. Never persist/return their text.
        return await finish("error")
