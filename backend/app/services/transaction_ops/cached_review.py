"""Historical observations for discovered review members, never fresh/write evidence.

Discovery still runs for uncovered windows. Only a supported, complete numeric
proof can replace per-order collection; all other members use the normal reader.
Copies and their cursor commit together without touching cases or approvals.
"""

from copy import deepcopy
from datetime import datetime
from uuid import UUID

from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import insert

from app.models.transaction_ops import TransactionFinding as Finding
from app.models.transaction_ops import TransactionRun as Run
from app.schemas.transaction_runs import FindingReport, ProgressUpdate
from app.services.transaction_ops import policy_equivalence as proof
from app.services.transaction_ops import state_service as state

MODE = "saved"
BATCH_SIZE = 100


def saved(run):
    return bool(run.params_json.get("review")) and run.params_json.get("evidence_mode") == MODE


def report_hash():
    return func.encode(func.sha256(func.convert_to(cast(Finding.report_json, Text), "UTF8")), "hex")


def equivalent(report, original, target, *, now, minimum_observed_at=None):
    original = {"evidence_contract_version": 1, "destination_discovery_version": 1, **original}
    if any(original.get(k) != target.get(k) for k in proof.SCOPE_KEYS):
        return None
    try:
        changed = (
            frozenset()
            if original.get("mapping_json") == target.get("mapping_json")
            else proof.changed_reasons(original, target)
        )
        result = proof.evaluate(report, original, changed, evaluated_at=now)
        if result["status"] != "equivalent":
            return None
        observed = datetime.fromisoformat(result["original_observed_at"])
        if minimum_observed_at is not None and observed < minimum_observed_at:
            return None
        return result
    except (ValueError, TypeError, KeyError):
        return None


class CachedReview:
    def __init__(self, db, tenant_id, run):
        self.db, self.tenant_id, self.run = db, tenant_id, run
        self.context = None
        self.prepared = {}

    async def prepare(self, references, progress, *, now):
        if not saved(self.run):
            return
        # Deletion pages use their read time; ordinary dependency/refund feeds
        # use the window end. Known source versions also invalidate old money.
        floor = self.run.params_json["window_end"]
        if progress.get("phase") == "destination" and progress.get("pending_evidence_since"):
            floor = progress["pending_evidence_since"]
        context = (progress.get("phase"), floor)
        if context != self.context:
            self.context, self.prepared = context, {}
        self.prepared = {ref: self.prepared[ref] for ref in references[:BATCH_SIZE] if ref in self.prepared}
        for ref, report in self.prepared.items():
            version = progress.get("pending_source_versions", {}).get(ref)
            if report is not None and version and progress.get("phase") == "orders":
                if datetime.fromisoformat(report["_observation"]["observed_at"]) < datetime.fromisoformat(version):
                    self.prepared[ref] = None
        refs = [ref for ref in references[:BATCH_SIZE] if ref not in self.prepared]
        if not refs:
            return
        config = await state.get_config(self.db, self.tenant_id, self.run.config_id)
        configs = [config.id, *([config.supersedes_config_id] if config.supersedes_config_id else [])]
        # Resolve run IDs first to avoid poor JSON-join cardinality estimates.
        run_ids = list(
            await self.db.scalars(
                select(Run.id)
                .where(
                    Run.tenant_id == self.tenant_id,
                    Run.config_id.in_(configs),
                    Run.status == "finished",
                    Run.origin != "recovery",
                    func.coalesce(Run.params_json["evidence_mode"].astext, "current") != MODE,
                )
                .order_by(Run.created_at.desc())
                .limit(512)
            )
        )
        self.prepared.update(dict.fromkeys(refs))
        if not run_ids:
            return
        picked = (
            select(Finding.id)
            .where(
                Finding.tenant_id == self.tenant_id,
                Finding.run_id.in_(run_ids),
                Finding.order_reference.in_(refs),
            )
            .distinct(Finding.order_reference)
            .order_by(Finding.order_reference, Finding.updated_at.desc(), Finding.id.desc())
            .cte("cached_review_candidates")
            .prefix_with("MATERIALIZED")
        )
        rows = (
            await self.db.execute(
                select(
                    Finding.id,
                    Finding.order_reference,
                    Finding.report_json,
                    report_hash().label("digest"),
                    Run.config_snapshot,
                )
                .join(picked, picked.c.id == Finding.id)
                .join(Run, (Run.id == Finding.run_id) & (Run.tenant_id == self.tenant_id))
                .where(Finding.tenant_id == self.tenant_id)
            )
        ).all()
        for row in rows:
            minimum = datetime.fromisoformat(floor) if floor else None
            version = progress.get("pending_source_versions", {}).get(row.order_reference)
            if progress.get("phase") == "orders" and version:
                version = datetime.fromisoformat(version)
                minimum = max(minimum, version) if minimum else version
            result = equivalent(
                row.report_json, row.config_snapshot, self.run.config_snapshot, now=now, minimum_observed_at=minimum
            )
            if result is None:
                continue
            # Carry numeric evidence only. Old AI recommendations, proposals,
            # write authority are deliberately not inherited. A case link remains
            # navigation only; the existing write path must obtain fresh proof.
            report = {
                key: deepcopy(row.report_json[key])
                for key in (
                    "order_reference",
                    "source",
                    "targets",
                    "refund_evidence",
                    "balance",
                    "_observation",
                    "case_id",
                )
                if key in row.report_json
            }
            report["_observation"] = {"final": True, "observed_at": result["original_observed_at"]}
            report["cached_evidence"] = {
                "finding_id": str(row.id),
                "input_hash": row.digest,
                "evaluated_at": now.isoformat(),
                "basis": "historical_as_observed",
                "rule_version": proof.RULE_VERSION,
            }
            report["next_action"] = "human_review" if result["balance"]["status"] != "matched" else "none"
            self.prepared[row.order_reference] = FindingReport(
                order_reference=row.order_reference, report_json=report
            ).report_json

    def prefix(self, references):
        reports = []
        for ref in references[:BATCH_SIZE]:
            report = self.prepared.get(ref)
            if report is None:
                break
            reports.append(report)
        return reports

    def fresh_prefix_size(self, references):
        count = 0
        for ref in references:
            if self.prepared.get(ref) is not None:
                break
            count += 1
        return count


async def persist(db, tenant_id, run_id, reports, *, lease_token, checkpoint, now):
    if not 1 <= len(reports) <= BATCH_SIZE or not isinstance(checkpoint, ProgressUpdate):
        raise ValueError("invalid_cached_review_batch")
    run = await state.get_run(db, tenant_id, run_id, lock=True)
    state._lease(run, lease_token, now)
    config = await state.get_config(db, tenant_id, run.config_id)
    if not saved(run) or not config.enabled or not await state.enabled_for_run(db, tenant_id):
        raise state.StateError("batch_disabled")
    refs = [r["order_reference"] for r in reports]
    pending = (run.progress_json or {}).get("pending_refs", [])
    if (
        len(set(refs)) != len(refs)
        or pending[: len(refs)] != refs
        or checkpoint.progress_json.get("pending_refs") != pending[len(refs) :]
        or checkpoint.progress_json.get("processed") != run.progress_json.get("processed", 0) + len(refs)
    ):
        raise ValueError("noncontiguous_cached_review_batch")
    originals = {UUID(r["cached_evidence"]["finding_id"]): r["cached_evidence"]["input_hash"] for r in reports}
    verified = dict(
        (
            await db.execute(
                select(Finding.id, report_hash())
                .where(Finding.tenant_id == tenant_id, Finding.id.in_(originals))
                .with_for_update(read=True)
            )
        ).all()
    )
    if verified != originals:
        # Concurrent replacement cannot publish an unpinned historical result.
        await state._commit(db, tenant_id)
        return False
    statement = insert(Finding).values(
        [dict(tenant_id=tenant_id, run_id=run.id, order_reference=r["order_reference"], report_json=r) for r in reports]
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=["tenant_id", "run_id", "order_reference"],
            set_={"report_json": statement.excluded.report_json, "updated_at": now},
        )
    )
    run.progress_json = checkpoint.progress_json
    run.lease_until = min(run.deadline_at, now + state._LEASE)
    await state._audit(
        db,
        tenant_id,
        "review.cached_evidence_reused",
        run,
        payload={"count": len(refs), "provider_calls": 0, "case_changes": 0},
    )
    await state._commit(db, tenant_id)
    return True
