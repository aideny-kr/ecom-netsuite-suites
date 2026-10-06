"""Original discovery membership, never monetary evidence or a new read clock.

Pages are committed with the collector's leased checkpoint. Only a completed
provider scan can seal them. Legacy or unsupported discovery retains its existing
fallback; final order snapshots cannot reconstruct original discovery dates.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, String, and_, case, cast, column, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import aliased

from app.models.transaction_evidence_batch import TransactionEvidenceBatch as Batch
from app.models.transaction_ops import TransactionRun as Run
from app.services.transaction_ops.evidence_batch import MAX_BYTES, context_hash, fingerprint
from app.services.transaction_ops.netsuite_changes import _REFERENCE
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

KEY = "period_membership"
MAX_PENDING_EVENTS = 5000


def calendar_aligned(root, span):
    from app.services.transaction_ops.periods import ReconciliationPolicy

    policy = ReconciliationPolicy.model_validate(
        root.config_snapshot.get("mapping_json", {}).get("reconciliation_policy") or {}
    )
    zone = ZoneInfo(policy.timezone_name)
    return all(value.astimezone(zone).time().isoformat() == "00:00:00" for value in (span.start, span.end))


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("unproven_membership_time")
    return parsed.astimezone(timezone.utc)


class Membership:
    def __init__(self, db, tenant, run, progress, clock):
        self.db, self.tenant, self.run, self.progress, self.clock = db, tenant, run, progress, clock
        self.pending = []
        self.credential = None
        original = run.progress_json or {}
        fresh = not any(
            original.get(key)
            for key in (
                "continuation_of",
                "evidence_root_id",
                "review_attempt",
                "last_source_id",
                "refund_after_id",
                "scan_count",
                "destination_scan_count",
                "dependency_scan",
                "processed",
                "scan_complete",
                "refund_scan_complete",
                "restart_scan",
            )
        )
        config = run.config_snapshot
        supported = (
            db is not None
            and fresh
            and bool(config.get("mapping_json", {}).get("metabase_replica"))
            and not run.params_json.get("order_references")
            and run.params_json.get("window_start")
            and run.params_json.get("window_end")
            and (
                run.params_json.get("window_basis", "updated_at") == "completed_at"
                or config.get("destination_discovery_version", 1) >= 2
            )
        )
        if KEY not in progress and supported:
            progress[KEY] = {
                "version": 1,
                "root": str(run.id),
                "context": context_hash(
                    config,
                    "period_membership_v1",
                    [
                        str(run.id),
                        run.params_json["window_start"],
                        run.params_json["window_end"],
                    ],
                ),
                "supported": True,
                "batches": 0,
            }

    @property
    def active(self):
        value = self.progress.get(KEY) or {}
        return value.get("version") == 1 and value.get("supported") is True

    def unsupported(self):
        if KEY in self.progress:
            self.progress[KEY]["supported"] = False
        self.pending.clear()

    def capture(self, references, at=None, *, deleted_at_raw=None):
        if not self.active or not references:
            return
        try:
            refs = sorted(set(references))
            if len(self.pending) + len(refs) > MAX_PENDING_EVENTS:
                raise ValueError("membership_page_too_large")
            if any(not isinstance(ref, str) or not _REFERENCE.fullmatch(ref) for ref in refs):
                raise ValueError("invalid_membership_reference")
            if deleted_at_raw is not None:
                stamp = datetime.fromisoformat(deleted_at_raw)
                if stamp.tzinfo is not None:
                    raise ValueError("deletion_time_must_remain_unzoned")
                event = {"deleted_at_raw": stamp.isoformat()}
            else:
                stamp = _time(at)
                if not _time(self.run.params_json["window_start"]) <= stamp < _time(self.run.params_json["window_end"]):
                    raise ValueError("membership_outside_window")
                event = {"at": stamp.isoformat()}
            self.pending.extend({"reference": ref, **event} for ref in refs)
        except (ValueError, TypeError, AttributeError):
            self.unsupported()

    def source(self, page, references):
        if self.active:
            basis = self.run.params_json.get("window_basis", "updated_at")
            for row in page["orders"]:
                if row["number"] in references:
                    self.capture([row["number"]], row.get(basis))

    def windows(self):
        from app.services.transaction_ops.periods import ReconciliationPolicy

        policy = ReconciliationPolicy.model_validate(
            self.run.config_snapshot["mapping_json"].get("reconciliation_policy") or {}
        )
        zone = ZoneInfo(policy.timezone_name)
        cursor, end = _time(self.run.params_json["window_start"]), _time(self.run.params_json["window_end"])
        windows = []
        while cursor < end and len(windows) < 8:
            following = min(
                end,
                (
                    cursor.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                ).astimezone(timezone.utc),
            )
            windows.append((cursor, following))
            cursor = following
        if len(windows) > 7:
            self.unsupported()
            return []
        return windows

    def change(self, refs, change, stream):
        if not self.active or not refs:
            return
        if stream not in {"transaction_lines", "transaction_links"}:
            self.capture(refs, change.get("modified_at"), deleted_at_raw=change.get("deleted_at_raw"))
            return
        windows = change.get("membership_windows")
        if not isinstance(windows, list) or not windows:
            self.unsupported()
            return
        if len(self.pending) + len(refs) * len(windows) > MAX_PENDING_EVENTS:
            self.unsupported()
            return
        for lower, upper in windows:
            if (
                _time(self.run.params_json["window_start"])
                <= _time(lower)
                < _time(upper)
                <= _time(self.run.params_json["window_end"])
            ):
                self.pending.extend({"reference": ref, "start": lower, "end": upper} for ref in refs)
            else:
                self.unsupported()
                return

    def refunds(self, page, references):
        if self.active:
            if "refunds" not in page or not set(references).issubset(
                {row.get("order_reference") for row in page.get("refunds", [])}
            ):
                self.unsupported()
                return
            for row in page["refunds"]:
                if row.get("order_reference") in references:
                    self.capture([row["order_reference"]], row.get("updated_at"))

    async def _put(self, value, identifier):
        meta = self.progress[KEY]
        encoded = json.dumps({"version": 1, "value": value}, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > MAX_BYTES:
            self.unsupported()
            return False
        if self.credential is None:
            try:
                self.credential = await fingerprint(
                    self.db,
                    self.tenant,
                    self.run.config_snapshot["netsuite_connection_id"],
                    self.run.config_snapshot["netsuite_account_id"],
                )
            except NetSuiteEvidenceError:
                # Optional candidate metadata must not preempt the original
                # reader's dispatchable-error / reactive auth recovery path.
                # Missing pages permanently disable this scan's extra proof.
                self.unsupported()
                return False
        now = self.clock()
        result = await self.db.execute(
            insert(Batch)
            .values(
                id=identifier,
                tenant_id=self.tenant,
                run_id=self.run.id,
                kind="dependencies",
                context_hash=meta["context"],
                connection_fingerprint=self.credential,
                started_at=now,
                completed_at=now,
                evidence_json=json.loads(encoded),
            )
            .on_conflict_do_nothing(index_elements=[Batch.id])
            .returning(Batch.id)
        )
        # The caller's lease-checked checkpoint commits these rows atomically.
        return result.scalar_one_or_none() is not None

    async def flush(self):
        if not self.active or not self.pending:
            self.pending.clear()
            return
        meta = self.progress[KEY]
        events = sorted({json.dumps(event, sort_keys=True) for event in self.pending})
        value = {"membership_version": 1, "root": meta["root"], "events": [json.loads(e) for e in events]}
        identifier = uuid5(
            NAMESPACE_URL, json.dumps([str(self.tenant), str(self.run.id), meta["context"], value], sort_keys=True)
        )
        if await self._put(value, identifier):
            meta["batches"] += 1
        self.pending.clear()

    async def seal(self):
        from app.services.transaction_ops.daily_evidence import reuses_coverage, scan_complete

        await self.flush()
        if not self.active or any(
            self.progress.get(key) is not None for key in ("reused_observation_run_ids", "reused_daily_run_ids")
        ):
            return
        proof = SimpleNamespace(
            status="finished",
            termination_reason="done",
            params_json=self.run.params_json,
            config_snapshot=self.run.config_snapshot,
            progress_json=self.progress,
        )
        if not scan_complete(proof) or reuses_coverage(proof):
            return
        meta = self.progress[KEY]
        await self._put(
            {"membership_version": 1, "root": meta["root"], "complete": True, "batches": meta["batches"]},
            UUID(meta["root"]),
        )


def sealed_membership(run=Run):
    """Tenant/scope-bound immutable seal from a completed provider-backed scan."""
    marker, proof, pages = aliased(Batch), aliased(Run), aliased(Batch)
    root = run.progress_json[KEY]["root"].astext
    safe_root = case((root.op("~")(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"), cast(root, PG_UUID)))
    body = marker.evidence_json["value"]
    page_count = (
        select(func.count())
        .where(
            pages.tenant_id == run.tenant_id,
            pages.kind == "dependencies",
            pages.context_hash == run.progress_json[KEY]["context"].astext,
            pages.evidence_json["value"]["membership_version"].astext == "1",
            pages.evidence_json["value"]["root"].astext == root,
            func.jsonb_typeof(pages.evidence_json["value"]["events"]) == "array",
        )
        .correlate(run)
        .scalar_subquery()
    )
    return (
        select(marker.id)
        .join(proof, and_(proof.id == marker.run_id, proof.tenant_id == run.tenant_id))
        .where(
            marker.id == safe_root,
            marker.tenant_id == run.tenant_id,
            marker.kind == "dependencies",
            marker.context_hash == run.progress_json[KEY]["context"].astext,
            run.progress_json[KEY]["version"].astext == "1",
            run.progress_json[KEY]["supported"].astext == "true",
            body["membership_version"].astext == "1",
            body["root"].astext == root,
            body["complete"].astext == "true",
            body["batches"] == func.to_jsonb(page_count),
            proof.config_id == run.config_id,
            proof.config_snapshot == run.config_snapshot,
            proof.params_json["window_start"] == run.params_json["window_start"],
            proof.params_json["window_end"] == run.params_json["window_end"],
            func.coalesce(proof.params_json["window_basis"].astext, "updated_at")
            == func.coalesce(run.params_json["window_basis"].astext, "updated_at"),
            proof.progress_json[KEY]["root"].astext == root,
            proof.progress_json[KEY]["supported"].astext == "true",
            proof.status == "finished",
            proof.termination_reason == "done",
            proof.progress_json["scan_complete"].astext == "true",
            proof.progress_json["refund_scan_complete"].astext == "true",
            *[
                proof.progress_json[key].astext.is_(None)
                for key in ("reused_observation_run_ids", "reused_daily_run_ids")
            ],
            # Only the supported complete dependency contract can seal updated-at discovery.
            (proof.params_json["window_basis"].astext == "completed_at")
            | and_(
                proof.progress_json["destination_scan_complete"].astext == "true",
                proof.progress_json["dependency_scan_complete"].astext == "true",
                proof.progress_json["dependency_index_seed"]["complete"].astext == "true",
            ),
        )
        .correlate(run)
        .exists()
    )


async def selected_members(db, tenant, runs, span):
    """Return fixed (run, reference) nominations, preserving raw deletion envelopes."""
    contexts = {row.progress_json[KEY]["context"] for row in runs}
    events = (
        func.jsonb_array_elements(
            case(
                (
                    func.jsonb_typeof(Batch.evidence_json["value"]["events"]) == "array",
                    Batch.evidence_json["value"]["events"],
                ),
                else_=literal([], type_=JSONB),
            )
        )
        .table_valued("value")
        .lateral()
    )
    event = cast(events.c.value, JSONB)
    at = cast(event["at"].astext, DateTime(timezone=True))
    lower, upper = (cast(event[key].astext, DateTime(timezone=True)) for key in ("start", "end"))
    deleted = cast(event["deleted_at_raw"].astext, DateTime(timezone=False))
    included = or_(
        and_(at >= span.start, at < span.end),
        and_(lower >= span.start, upper <= span.end, lower < upper),
        and_(
            deleted >= span.start.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=2),
            deleted < span.end.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(days=2),
        ),
    )
    rows = (
        await db.execute(
            select(Batch.context_hash, event["reference"].astext.label("reference"))
            .join(events, true())
            .where(
                Batch.tenant_id == tenant,
                Batch.kind == "dependencies",
                Batch.context_hash.in_(contexts),
                Batch.evidence_json["value"]["membership_version"].astext == "1",
                func.jsonb_typeof(Batch.evidence_json["value"]["events"]) == "array",
                included,
            )
            .distinct()
        )
    ).all()
    # Keep run -> context and context -> references separate. Expanding their
    # cross product in Python grows with every continuation and can exhaust RAM.
    return {
        "runs": [{"run_id": str(row.id), "context": row.progress_json[KEY]["context"]} for row in runs],
        "members": [{"context": context, "order_reference": ref} for context, ref in rows],
    }


def member_filter(finding, pairs, name):
    runs = (
        func.jsonb_to_recordset(literal(pairs["runs"], type_=JSONB))
        .table_valued(
            column("run_id", PG_UUID),
            column("context", String),
        )
        .render_derived(name=f"{name}_runs", with_types=True)
    )
    rows = (
        func.jsonb_to_recordset(literal(pairs["members"], type_=JSONB))
        .table_valued(
            # Two compact JSON binds stay below the driver's parameter ceiling.
            column("context", String),
            column("order_reference", String),
        )
        .render_derived(name=name, with_types=True)
    )
    return (
        select(literal(1))
        .select_from(runs.join(rows, rows.c.context == runs.c.context))
        .where(
            runs.c.run_id == finding.run_id,
            rows.c.order_reference == finding.order_reference,
        )
        .correlate(finding)
        .exists()
    )
