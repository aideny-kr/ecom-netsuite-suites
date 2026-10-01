"""Durable, task-local accounting for BigQuery reads inside scheduled reports.

No query text or credentials are persisted here. A pending receipt means the
provider may have spent money; it must never become zero usage or a blind retry.
USD is intentionally unsupported as a ceiling without an account pricing contract.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.job import Job


class ReportQueryBudgetError(RuntimeError):
    pass


class ReportQueryUnknownError(RuntimeError):
    pass


_scope = ContextVar("scheduled_report_queries", default=None)


def current_report_queries():
    return _scope.get()


def query_bytes(summary):
    return sum(
        q.get("bytes_processed", 0) for q in (summary or {}).get("report_queries", []) if q.get("state") == "complete"
    )


def queries_uncertain(summary):
    return any(q.get("state") != "complete" for q in (summary or {}).get("report_queries", []))


class ReportQueries:
    def __init__(self, ctx):
        self.ctx = ctx
        self.bytes_processed = 0
        self.error = None
        self.pending = None

    async def _job(self):
        await set_tenant_context(self.ctx.db, str(self.ctx.tenant_id))
        job = await self.ctx.db.scalar(
            select(Job)
            .where(Job.id == self.ctx.run_id, Job.tenant_id == self.ctx.tenant_id)
            .execution_options(populate_existing=True)
        )
        if job is None or job.status != "running":
            raise ReportQueryUnknownError("Report query requires its running job")
        return job

    def check(self):
        if self.pending is not None:
            raise ReportQueryUnknownError("Report query outcome or usage is unknown; reconciliation required")
        if self.error:
            raise ReportQueryBudgetError(self.error)

    async def begin(self, *, source_id=None, query_sha256=None):
        self.check()
        if self.ctx.budget.get("usd") is not None:
            self.error = "Report USD ceilings require an account pricing contract; use scan/time limits"
            self.check()
        job = await self._job()
        summary = dict(job.result_summary or {})
        if queries_uncertain(summary):
            raise ReportQueryUnknownError("Previous report query usage is unknown")
        # The same run may contain several reports. All internal queries consume
        # one shared scan ceiling, including successful reads of a failed report.
        limit = self.ctx.budget.get("bytes_scanned")
        other_bytes = sum(
            int(a.get("bytes_processed") or 0) - int(a.get("report_query_bytes") or 0)
            for a in self.ctx.artifacts.values()
        )
        remaining = None if limit is None else int(limit - query_bytes(summary) - other_bytes)
        if remaining is not None and remaining <= 0:
            self.error = "Report scan budget exhausted before next query"
            self.check()
        cap = min(1_000_000_000, remaining) if remaining is not None else 1_000_000_000
        receipts = list(summary.get("report_queries", []))
        self.pending = len(receipts)
        receipts.append(
            {
                "step_id": self.ctx.current_step_id,
                "state": "pending",
                "maximum_bytes_billed": cap,
                "source_id": source_id,
                "query_sha256": query_sha256,
            }
        )
        job.result_summary = {**summary, "report_queries": receipts}
        await self.ctx.db.commit()  # intent is durable before provider dispatch
        return cap

    async def complete(self, result):
        processed = result.get("bytes_processed") if isinstance(result, dict) else None
        billed = result.get("bytes_billed") if isinstance(result, dict) else None
        if not isinstance(processed, int) or isinstance(processed, bool) or processed < 0:
            self.check()  # keep the pending reservation; never coerce absent usage to zero
        if billed is not None and (not isinstance(billed, int) or isinstance(billed, bool) or billed < 0):
            self.check()
        job = await self._job()
        summary = dict(job.result_summary or {})
        receipts = list(summary.get("report_queries", []))
        if self.pending is None or self.pending >= len(receipts) or receipts[self.pending]["state"] != "pending":
            raise ReportQueryUnknownError("Report query reservation changed")
        receipts[self.pending] = {
            **receipts[self.pending],
            "state": "complete",
            "bytes_processed": processed,
            "bytes_billed": billed,
            "cache_hit": result.get("cache_hit") is True,
            "provider_job_id": result.get("job_id") if isinstance(result.get("job_id"), str) else None,
        }
        job.result_summary = {**summary, "report_queries": receipts}
        await self.ctx.db.commit()
        self.pending = None
        self.bytes_processed += processed
        limit = self.ctx.budget.get("bytes_scanned")
        if billed is not None and billed > receipts[-1]["maximum_bytes_billed"]:
            self.error = "Provider query receipt exceeds its billing cap"
        if limit is not None and query_bytes(job.result_summary) > limit:
            self.error = "Report scan budget exceeded"
        self.check()


@contextmanager
def report_queries(ctx):
    scope = ReportQueries(ctx)
    token = _scope.set(scope)
    try:
        yield scope
        scope.check()  # survives tool wrappers that convert exceptions to results
    finally:
        _scope.reset(token)
