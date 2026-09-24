"""Worker-local adapter for bounded durable batches; single reads remain fallback."""

from app.services.transaction_ops import evidence_batch as store
from app.services.transaction_ops import netsuite_bulk as bulk
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
from app.services.transaction_ops.normalization import _time


class StagedNetSuite:
    def __init__(self, db, tenant, run, config, mapping, progress, *, clock, reserve, read, checkpoint):
        self.db, self.tenant, self.run = db, tenant, run
        self.config, self.mapping, self.progress = config, mapping, progress
        self.clock, self.reserve, self.read, self.checkpoint = clock, reserve, read, checkpoint
        self.cache, self.attempted = {}, set()
        self.order_id = None

    def scope(self):
        from uuid import UUID

        c = self.config
        return self.db, self.tenant, UUID(c["netsuite_connection_id"]), c["netsuite_account_id"], c["subsidiary_id"]

    async def get(self, kind, reference, context, factory, *, can_fetch=True):
        now = self.clock()
        since = max(_time(self.progress.get("continuation_started_at")) or self.run.created_at, now - store.MAX_AGE)
        key = (kind, context)
        entry = self.cache.get(key)
        if entry is None or entry[2] < since:
            identifier = self.progress.get("native_" + kind + "_batch")
            data = (
                await store.load(self.db, self.tenant, identifier, kind, context, self.config, since=since, now=now)
                or {}
            )
            # A durable hit's original observation is checked in load. Do not
            # extend its in-memory lifetime past this same invocation's floor.
            entry = (identifier, data, since)
            self.cache[key] = entry
        identifier, data, _ = entry
        if reference not in data and can_fetch and key not in self.attempted:
            if await self.reserve(bulk.MAX_CALLS + 3, hold=True):
                self.attempted.add(key)
                started = self.clock()
                try:
                    result = await self.read(
                        "netsuite_" + kind + "_batch", factory, held=bulk.MAX_CALLS + 3, data_calls=bulk.MAX_CALLS
                    )
                except (NetSuiteEvidenceError, ValueError):
                    counter = "native_" + kind + "_batch_failures"
                    self.progress[counter] = self.progress.get(counter, 0) + 1
                    self.cache[key] = (None, {}, started)
                    return None
                identifier = await store.save(
                    self.db,
                    self.tenant,
                    self.run.id,
                    kind,
                    context,
                    self.config,
                    result,
                    started_at=started,
                    now=self.clock(),
                )
                data = result[kind]
                self.cache[key] = (identifier, data, started)
                self.progress["native_" + kind + "_batch"] = identifier
                counter = "native_" + kind + "_batches"
                self.progress[counter] = self.progress.get(counter, 0) + 1
                await self.checkpoint()
        if reference in data:
            counter = "native_" + kind + "_batch_hits"
            self.progress[counter] = self.progress.get(counter, 0) + 1
            if kind == "orders":
                self.order_id = identifier
            return data[reference]
        return None

    async def order(self, reference):
        # Include this page's references so advancing the source/dependency feed
        # never reuses a negative result or an older parent-only observation.
        refs = list(dict.fromkeys(self.progress["pending_refs"][: bulk.MAX_ORDERS]))
        if not refs:
            return None
        phase = self.progress.get("phase")
        # The persisted context must survive consuming the front of this batch.
        # Resolve the current manifest first using only config + scan phase.
        context = store.context_hash(self.config, phase)
        key = ("orders", context)
        cached = self.cache.get(key)
        if cached and cached[1] and reference not in cached[1]:
            self.cache.clear()
            self.attempted.clear()
        return await self.get(
            "orders",
            reference,
            context,
            lambda: bulk.read_orders(*self.scope(), refs, self.mapping.reference_field),
            can_fetch=len(refs) > 1,
        )

    async def refund(self, reference, target):
        phase = self.progress.get("phase")
        entry = self.cache.get(("orders", store.context_hash(self.config, phase)))
        if not entry or not self.order_id or entry[1].get(reference) != target:
            return None
        context = store.context_hash(self.config, phase, self.order_id)
        eligible = {
            ref: value
            for ref, value in entry[1].items()
            if value.get("complete") is True and value.get("lookup", {}).get("count") == 1
        }
        if reference not in eligible:
            return None
        return await self.get(
            "refunds",
            reference,
            context,
            lambda: bulk.read_refunds(
                *self.scope(),
                eligible,
                adjustment_profile=self.mapping.refund_adjustments.model_dump(mode="json")
                if self.mapping.refund_adjustments
                else None,
            ),
        )
