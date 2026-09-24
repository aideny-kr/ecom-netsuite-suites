"""Bounded duration counters; no provider payloads, references or additional I/O."""

import time
from contextlib import contextmanager

_STATES = frozenset(
    {
        "get_config",
        "reserve_budget",
        "settle_budget",
        "update_progress",
        "record_finding",
        "propose",
    }
)
_READS = frozenset(
    {
        "source_page",
        "source_order",
        "source_refunds",
        "netsuite_order",
        "netsuite_refunds",
        "commercial_credit",
        "create_preview",
        "guard_snapshot",
        "celigo_error",
        "dependency_page",
        "dependency_owners",
        "refund_page",
        "replica_page",
    }
)


class RunTiming:
    def __init__(self, progress, *, clock=time.monotonic):
        self.progress, self.clock = progress, clock
        self.started = clock()
        # Per-slice, rather than inherited totals: queue waiting is outside this clock.
        self.values = {}
        progress["timing_ms"] = self.values

    def snapshot(self):
        # Validated checkpoints copy nested JSON; restore the live counters.
        self.progress["timing_ms"] = self.values
        self.progress["active_ms"] = max(0, round((self.clock() - self.started) * 1000))

    @contextmanager
    def measure(self, stage):
        start = self.clock()
        try:
            yield
        finally:
            key = stage if stage in _STATES | _READS | {"enablement", "source_mirror", "source_snapshot"} else "other"
            value = self.values.setdefault(key, {"calls": 0, "total": 0, "max": 0})
            elapsed = max(0, round((self.clock() - start) * 1000))
            value["calls"] += 1
            value["total"] += elapsed
            value["max"] = max(value["max"], elapsed)

    def state(self, original):
        timing = self

        class TimedState:
            def __getattr__(self, name):
                method = getattr(original, name)
                if name not in _STATES:
                    return method

                async def call(*args, **kwargs):
                    with timing.measure(name):
                        return await method(*args, **kwargs)

                return call

        return TimedState()
