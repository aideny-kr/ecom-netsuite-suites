"""Immutable discovery pages in existing PostgreSQL storage, not financial evidence.

Only a small reference enters the leased run checkpoint. The original page and
native owner-query rows survive continuations; local consumption never repeats
those reads. Current connection access is reauthorized on load. Token rotation
must not erase a saved candidate inventory (the old inline checkpoints likewise
survived refresh); its credential fingerprint remains immutable provenance.
"""

import copy
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.transaction_evidence_batch import TransactionEvidenceBatch as Batch
from app.models.transaction_ops import TransactionRun
from app.services.transaction_ops.evidence_batch import MAX_BYTES, context_hash, fingerprint
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError


class DependencyStaging:
    def __init__(self, db, tenant, run, config, progress, clock):
        self.db, self.tenant, self.run, self.config, self.clock = db, tenant, run, config, clock
        root = progress.setdefault("dependency_stage_root", str(run.id))
        self.context = context_hash(
            config, "dependency_discovery_v1", [root, run.params_json["window_start"], run.params_json["window_end"]]
        )
        self.cache = {}

    async def put(self, value):
        encoded = json.dumps({"version": 1, "value": value}, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > MAX_BYTES:
            raise NetSuiteEvidenceError("dependency_stage_size")
        credential = await fingerprint(
            self.db, self.tenant, self.config["netsuite_connection_id"], self.config["netsuite_account_id"]
        )
        if not await self.db.scalar(
            select(TransactionRun.id).where(TransactionRun.tenant_id == self.tenant, TransactionRun.id == self.run.id)
        ):
            raise NetSuiteEvidenceError("invalid_batch_run")
        identifier = uuid5(NAMESPACE_URL, json.dumps([str(self.tenant), str(self.run.id), self.context, encoded]))
        now = self.clock()
        await self.db.execute(
            insert(Batch)
            .values(
                id=identifier,
                tenant_id=self.tenant,
                run_id=self.run.id,
                kind="dependencies",
                context_hash=self.context,
                connection_fingerprint=credential,
                started_at=now,
                completed_at=now,
                evidence_json=json.loads(encoded),
            )
            .on_conflict_do_nothing(index_elements=[Batch.id])
        )
        await self.db.commit()
        self.cache[str(identifier)] = copy.deepcopy(value)
        # At most the current page and its current owner chunk are hot.
        while len(self.cache) > 2:
            self.cache.pop(next(iter(self.cache)))
        return {"stage_ref": str(identifier)}

    async def get(self, value):
        if not isinstance(value, dict) or "stage_ref" not in value:
            return copy.deepcopy(value)  # Compatible with an in-flight legacy page.
        identifier = str(UUID(value["stage_ref"]))
        await fingerprint(
            self.db, self.tenant, self.config["netsuite_connection_id"], self.config["netsuite_account_id"]
        )
        if identifier in self.cache:
            return copy.deepcopy(self.cache[identifier])
        row = await self.db.scalar(
            select(Batch).where(
                Batch.id == UUID(identifier),
                Batch.tenant_id == self.tenant,
                Batch.kind == "dependencies",
                Batch.context_hash == self.context,
            )
        )
        if row is None or row.evidence_json.get("version") != 1:
            raise NetSuiteEvidenceError("dependency_stage_scope_changed")
        result = row.evidence_json["value"]
        self.cache[identifier] = copy.deepcopy(result)
        while len(self.cache) > 2:
            self.cache.pop(next(iter(self.cache)))
        return copy.deepcopy(result)
