"""Write adapters for the scheduled transaction-ops path.

An adapter owns the provider-specific side of one attempt: the budgeted reads that
re-establish the approved evidence (``preflight``), the one send behind the one-use permit
(``send``) and the independent readback (``verify``). It never writes the ledger; the
kernel (write_kernel.py) does that from the adapter's results.

The reads and the dispatch are injected (``Reads``) rather than imported, so the executor
module keeps owning the names its tests patch, and a future adapter can be exercised with
fakes without a provider.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.services.transaction_ops.create_verification import verify_created_outcome
from app.services.transaction_ops.netsuite_create import prepare_create_input
from app.services.transaction_ops.normalization import _time
from app.services.transaction_ops.planner import plan_proposal, source_fingerprint
from app.services.transaction_ops.runner import build_report
from app.services.transaction_ops.source_eligibility import FAILED_PAYMENT, payment_failed
from app.services.transaction_ops.write_kernel import ExecutionStoppedError, PreconditionChangedError

Reader = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Reads:
    """The provider reads and the one dispatch an adapter is allowed to use."""

    source: Reader
    target: Reader
    guard: Reader
    create_preview: Reader
    created_snapshot: Reader
    dispatch_netsuite: Reader
    celigo_evidence: Reader
    celigo_resolution: Reader
    dispatch_celigo: Reader
    max_guard_calls: int
    max_celigo_calls: int


def verify_outcome(proposal, report, *, guard=None, resolution=None, creation=None, now=None):
    """Proof of the approved desired state, never proof inferred from a receipt."""
    if proposal.action == "sync_missing_order":
        return verify_created_outcome(
            proposal, report, guard=guard, creation=creation, now=now or datetime.now(timezone.utc)
        )
    evidence = proposal.evidence_json
    if (
        evidence.get("schema_version") != 1
        or source_fingerprint(report["source"]) != source_fingerprint(evidence["report"]["source"])
        or report["comparison"]["recommended_action"] != "no_action"
        or len(report["targets"]) != 1
        or report["targets"][0]["record_id"] != proposal.target_record_id
    ):
        return None
    proof = {"source_unchanged": True, "report": report}
    if proposal.action == "correct_amounts":
        if not guard or not isinstance(guard.get("snapshot"), dict):
            return None
        expected = deepcopy(proposal.before_json)
        expected.update(proposal.after_json["body_changes"])
        expected.update(proposal.after_json["expected_totals"])
        lines = {line["line"]: line for line in expected["lines"]}
        for change in proposal.after_json["line_changes"]:
            lines[change["line"]].update(change["fields"])
        actual = deepcopy(guard["snapshot"])
        if _time(actual.get("version")) != _time(report["targets"][0]["updated_at"]):
            return None
        expected.pop("version")
        actual.pop("version")
        if actual != expected:
            return None
        proof["guard"] = guard
    elif proposal.action == "resolve_celigo_error":
        approved = evidence.get("celigo") or {}
        if not resolution or (
            resolution.get("complete") is not True
            or resolution.get("resolved") is not True
            or resolution.get("error_id") != (approved.get("error") or {}).get("error_id")
            or resolution.get("order_reference") != proposal.order_reference
            or resolution.get("scope") != approved.get("scope")
            or resolution.get("config_fingerprint") != approved.get("config_fingerprint")
        ):
            return None
        proof["celigo"] = resolution
    else:
        return None
    return proof


@dataclass
class _ScheduledAdapter:
    """What the two scheduled-path adapters share: the source/target pair and the plan check."""

    reads: Reads
    config: Any
    mapping: Any
    proposal: Any
    clock: Callable[[], datetime]

    async def pair(self, db, tenant_id, read):
        config, mapping, proposal = self.config, self.mapping, self.proposal
        source = await read(
            2,
            self.reads.source,
            config.source_step_id,
            proposal.order_reference,
            **({"include_sync_data": True} if mapping.line_identity_mode == "inventory_units" else {}),
            **(
                {"source_connection_id": config.source_connection_id}
                if getattr(config, "source_connection_id", None)
                else {}
            ),
        )
        if len(source.get("orders") or []) == 1 and payment_failed(source["orders"][0]):
            raise ExecutionStoppedError(FAILED_PAYMENT, keep_code=True)
        targets = await read(
            10,
            self.reads.target,
            config.netsuite_connection_id,
            config.netsuite_account_id,
            config.subsidiary_id,
            proposal.order_reference,
            mapping.reference_field,
        )
        scope = {key: getattr(config, key) for key in ("netsuite_account_id", "subsidiary_id", "record_type")}
        report = build_report(source, targets, scope, mapping, now=self.clock())
        if report["order_reference"] != proposal.order_reference:
            raise ExecutionStoppedError("source_identity_changed")
        return source, targets, report

    def _same_plan(self, claimed, report, targets, **evidence):
        fresh = plan_proposal(report, targets, self.config, now=self.clock(), **evidence)
        if (
            fresh.evidence_fingerprint != self.proposal.evidence_fingerprint
            or fresh.before_json != claimed.before_json
            or fresh.after_json != claimed.after_json
        ):
            raise PreconditionChangedError("approved_evidence_changed")


class GuardRestletAdapter(_ScheduledAdapter):
    """correct_amounts and sync_missing_order through the guard RESTlet."""

    name = "guard_restlet"
    provider = "netsuite"

    async def preflight(self, db, tenant_id, claimed, *, read):
        source, targets, report = await self.pair(db, tenant_id, read)
        guard = creation = None
        if claimed.action == "correct_amounts":
            guard = await read(self.reads.max_guard_calls, self.reads.guard, self.config, claimed.target_record_id)
        elif claimed.action == "sync_missing_order":
            if report["comparison"]["recommended_action"] != "propose_missing_sync":
                raise PreconditionChangedError("approved_evidence_changed")
            creation = self._creation(source)
            guard = await read(
                self.reads.max_guard_calls, self.reads.create_preview, self.config, creation.payload_json
            )
        else:
            raise ExecutionStoppedError("unsupported_action", keep_code=True)
        self._same_plan(claimed, report, targets, guard=guard, creation=creation)
        return {"guard": guard, "creation": creation}

    async def send(self, db, tenant_id, claimed, preflight):
        # The dispatcher owns the final live guard and the committed one-use permit.
        return await self.reads.dispatch_netsuite(db, tenant_id, claimed)

    async def verify(self, db, tenant_id, claimed, preflight, *, read):
        source, _, report = await self.pair(db, tenant_id, read)
        guard = creation = None
        if claimed.action == "correct_amounts":
            guard = await read(self.reads.max_guard_calls, self.reads.guard, self.config, claimed.target_record_id)
        else:
            creation = self._creation(source)
            if len(report["targets"]) == 1:
                guard = await read(
                    self.reads.max_guard_calls,
                    self.reads.created_snapshot,
                    self.config,
                    report["targets"][0]["record_id"],
                    claimed.after_json,
                )
        return verify_outcome(self.proposal, report, guard=guard, creation=creation, now=self.clock())

    def _creation(self, source):
        return prepare_create_input(
            source,
            self.mapping,
            account_id=self.config.netsuite_account_id,
            subsidiary_id=self.config.subsidiary_id,
            now=self.clock(),
        )


class CeligoAdapter(_ScheduledAdapter):
    """resolve_celigo_error through the Celigo error API."""

    name = "celigo"
    provider = "celigo"

    async def preflight(self, db, tenant_id, claimed, *, read):
        _, targets, report = await self.pair(db, tenant_id, read)
        celigo = await read(
            self.reads.max_celigo_calls,
            self.reads.celigo_evidence,
            self.config.target_step_id,
            self.proposal.order_reference,
            error_id=self.proposal.before_json["celigo_error_id"],
        )
        self._same_plan(claimed, report, targets, celigo=celigo)
        return {"celigo": celigo}

    async def send(self, db, tenant_id, claimed, preflight):
        return await self.reads.dispatch_celigo(db, tenant_id, claimed, preflight["celigo"])

    async def verify(self, db, tenant_id, claimed, preflight, *, read):
        _, _, report = await self.pair(db, tenant_id, read)
        resolution = await read(
            self.reads.max_celigo_calls,
            self.reads.celigo_resolution,
            self.config.target_step_id,
            self.proposal.evidence_json["celigo"],
        )
        return verify_outcome(self.proposal, report, resolution=resolution, now=self.clock())


ADAPTERS = {
    "correct_amounts": GuardRestletAdapter,
    "sync_missing_order": GuardRestletAdapter,
    "resolve_celigo_error": CeligoAdapter,
}


def build_adapter(action, **kwargs):
    """The adapter for a proposal action; the registry, not the caller, decides."""
    try:
        cls = ADAPTERS[action]
    except KeyError:
        raise ExecutionStoppedError("unsupported_action", keep_code=True) from None
    return cls(**kwargs)
