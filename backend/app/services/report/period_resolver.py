# backend/app/services/report/period_resolver.py
"""Resolve the last CLOSED NetSuite accounting period for a tenant.

The dashboard wall should track "the last closed period," not the newest period
NetSuite happens to have created (future periods already exist and are wide
open) and not "the most recently ended calendar month" (books close days-to-
weeks after month end, so last month can still be open). Only
`accountingperiod.closed = 'T'` answers the question that matters.

Grounded in a live query against the Framework tenant (2026-08-05):
  - `Jun 2026` -> closed='T'.  `Jul 2026` has ENDED on the calendar but is
    closed='F' — still open in NetSuite.
  - Periods already exist through `Dec 2026`, all closed='F' — "newest period"
    picks a wide-open future month.
  - An `Adjust 2025` row exists with isadjust='T' whose name is not "Mon YYYY"
    at all — it must never be mistaken for a real period.
  - `closed` / `isadjust` / `isquarter` / `isyear` are the STRINGS 'T'/'F' on
    this table, not booleans — SuiteQL has no boolean type.

Query path: the local SuiteQL tool (`netsuite_suiteql.execute`), the same one
statement refreshes already use — `accountingperiod` is in
`NETSUITE_SUITEQL_ALLOWED_TABLES` (config.py). Precedent for a service calling
it directly (no MCP round-trip): `metric_compute.py::_validate_and_execute_by_source`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.mcp.tools import netsuite_suiteql
from app.mcp.tools.netsuite_financial_report import _PERIOD_NAME_RE

# Only 1 row is ever needed per query (newest-closed, or nearest-next-open) —
# both the SQL's own FETCH FIRST and this params limit agree, so
# netsuite_suiteql.enforce_limit() has nothing to cap.
_ROW_LIMIT = 1


class PeriodUnavailableReason(str, Enum):
    """Why `resolve_last_closed_period` could not produce a usable closed period.

    Never raised as an exception — always surfaced on the `ClosedPeriod` result
    object so the caller (the dashboard wall) can degrade instead of 500ing.
    """

    NO_CLOSED_PERIOD = "no_closed_period"
    UNSUPPORTED_PERIOD_NAME = "unsupported_period_name"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class ClosedPeriod:
    """Result of resolving the last closed period. Always returned — never
    `None`, never raised.

    This is a "None-equivalent" result object: a RESOLVED result has
    `name`/`enddate` set and `reason` is `None`; an UNRESOLVED result has
    `name`/`enddate`/`next_open_*` all `None` and `reason` set to why. Check
    `.resolved` (or `reason is None`) rather than truthiness — a frozen
    dataclass instance is always truthy in an `if result:` check, which would
    silently treat the unresolved shape as success.

    `next_open_name`/`next_open_enddate` are populated only when resolved AND
    a later, still-open period exists — the wall's ribbon reads them to say
    "Jul 2026 is still open." Their absence (both None) on an otherwise
    resolved result means either there's no later period yet, or the
    secondary lookup itself failed (degrades gracefully — see
    `resolve_last_closed_period`), not that anything is wrong with `name`.
    """

    name: str | None
    enddate: date | None
    next_open_name: str | None = None
    next_open_enddate: date | None = None
    reason: PeriodUnavailableReason | None = None

    @property
    def resolved(self) -> bool:
        return self.reason is None


def _unavailable(reason: PeriodUnavailableReason) -> ClosedPeriod:
    return ClosedPeriod(name=None, enddate=None, reason=reason)


def _parse_period_date(value: object) -> date:
    """Parse a SuiteQL DATE column value into a `date`.

    NetSuite's REST SuiteQL path returns DATE columns as ISO 'YYYY-MM-DD'
    strings. Deliberately narrow: a value that isn't a parseable ISO date is a
    schema-drift condition, not one of our three defined reasons — it
    propagates (a genuinely unexpected shape) rather than being mislabeled
    'unreachable', which would claim a connectivity problem that didn't happen.
    """
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def _first_row(result: dict) -> dict[str, object] | None:
    """Pull the single row out of a netsuite_suiteql.execute() success dict,
    keyed by lowercased column name (defensive against alias-casing — see
    reorder_columns/collect_columns in netsuite_suiteql.py)."""
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not columns or not rows:
        return None
    return {str(col).lower(): val for col, val in zip(columns, rows[0])}


async def _query_one_period(db, tenant_id, *, where: str, order: str) -> dict:
    query = (
        f"SELECT periodname, enddate FROM accountingperiod "
        f"WHERE {where} ORDER BY enddate {order} FETCH FIRST 1 ROWS ONLY"
    )
    return await netsuite_suiteql.execute(
        {"query": query, "limit": _ROW_LIMIT},
        {"tenant_id": str(tenant_id), "db": db},
    )


async def resolve_last_closed_period(db, tenant_id) -> ClosedPeriod:
    """Resolve the last closed accounting period for `tenant_id`, plus whether a
    later period is still open.

    Never raises for "couldn't determine": a SuiteQL/connection failure on the
    primary lookup returns `reason=UNREACHABLE`; an empty result returns
    `reason=NO_CLOSED_PERIOD`; a period name that doesn't satisfy
    `netsuite_financial_report`'s `"Mon YYYY"` validator (13-period / 4-4-5
    fiscal calendars, renamed periods) returns `reason=UNSUPPORTED_PERIOD_NAME`
    rather than reformatting or coercing it — a name that would fail
    downstream validation is worse than no name at all. Genuinely unexpected
    programming errors (a malformed `context`, a non-ISO date value, etc.) are
    NOT one of those three cases and are allowed to propagate.
    """
    # All four filters matter: `closed = 'T'` excludes every still-open period
    # (including the future months NetSuite already created through Dec 2026);
    # `isadjust = 'F'` excludes adjustment postings like `Adjust 2025`, whose
    # name doesn't even look like a period; `isquarter = 'F'` / `isyear = 'F'`
    # exclude the quarter/year ROLLUP rows accountingperiod also carries
    # (same enddate as their last constituent month, so without these two
    # filters a rollup row can out-rank the real month on "newest by enddate").
    closed_result = await _query_one_period(
        db,
        tenant_id,
        where="closed = 'T' AND isadjust = 'F' AND isquarter = 'F' AND isyear = 'F'",
        order="DESC",
    )
    if closed_result.get("error"):
        return _unavailable(PeriodUnavailableReason.UNREACHABLE)

    closed_row = _first_row(closed_result)
    if closed_row is None:
        return _unavailable(PeriodUnavailableReason.NO_CLOSED_PERIOD)

    raw_name = closed_row.get("periodname")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not _PERIOD_NAME_RE.match(name):
        return _unavailable(PeriodUnavailableReason.UNSUPPORTED_PERIOD_NAME)

    enddate = _parse_period_date(closed_row["enddate"])

    # Second lookup: the next period after the one we just resolved, restricted
    # to closed='F' (it's known to be open by construction — nothing later than
    # the row above passed the closed='T' filter — but stating it explicitly
    # keeps the intent readable and is harmless either way) and the same
    # isadjust/isquarter/isyear exclusions, so a bookkeeping adjustment or a
    # rollup row can't be reported as "the next open period."
    open_result = await _query_one_period(
        db,
        tenant_id,
        where=(
            f"enddate > TO_DATE('{enddate.isoformat()}', 'YYYY-MM-DD') "
            "AND closed = 'F' AND isadjust = 'F' AND isquarter = 'F' AND isyear = 'F'"
        ),
        order="ASC",
    )
    if open_result.get("error"):
        # We already have a valid closed period — a failure on the secondary
        # "is there a later open period" lookup degrades to "can't tell" on
        # that detail only. It must not discard the closed period we already
        # resolved; that would turn a partial answer into a manufactured
        # UNREACHABLE for a query that actually succeeded.
        return ClosedPeriod(name=name, enddate=enddate)

    next_open_row = _first_row(open_result)
    next_open_name: str | None = None
    next_open_enddate: date | None = None
    if next_open_row is not None:
        raw_next_name = next_open_row.get("periodname")
        next_open_name = raw_next_name.strip() if isinstance(raw_next_name, str) else None
        next_open_enddate = _parse_period_date(next_open_row["enddate"])

    return ClosedPeriod(
        name=name,
        enddate=enddate,
        next_open_name=next_open_name,
        next_open_enddate=next_open_enddate,
    )
