from pydantic import BaseModel, Field

from app.schemas.report import ReportResponse


class DashboardSeriesResponse(BaseModel):
    """A tracking series (rolling-period Stage 1, Task 4) as a switcher entry — mock
    §5's "Tracking the close" group. `period` is the newest linked report's own "Mon
    YYYY" period, the same value the switcher shows as its `meta`; `period` and
    `report_id` are both None only when the series exists (Task 3's tracking compose
    get-or-creates it up front) but has composed no report yet — a real, momentarily
    empty state, not an error."""

    id: str
    playbook_key: str
    period: str | None = None
    report_id: str | None = None


class DashboardTrackingInfo(BaseModel):
    """Extra ribbon context on `DashboardResponse`, present ONLY when `active` resolved
    from a TRACKING (series) selection — entirely absent for a snapshot/report
    selection (mock §3: "a pinned snapshot shows no ribbon"). Backs the ribbon's green
    and grey states. The amber "{period} closed {n} days ago — building {month}'s
    statement now" state needs Stage 2's scheduled compose and has no backing field
    here yet — the frontend must never infer it from this shape's absence, only render
    it if/when a future field says so.

    `period` is the ACTIVE report's own period ("Mon YYYY") — present whenever a
    report exists for the series, independent of whether the live NetSuite check below
    succeeded; None when the series has composed no report yet.

    `period_check_ok` is True only when the one-per-GET call to
    `period_resolver.resolve_last_closed_period` both ran and resolved. It is False
    for two collapsed cases that the ribbon treats identically ("can't tell"): the
    resolver degraded (NetSuite unreachable, no closed period, or a non-conforming
    period name — see `PeriodUnavailableReason`), or there was no report to check
    against in the first place (so the call was skipped rather than wasted).
    `resolved_period` / `next_open_period` are only ever set when `period_check_ok`.
    """

    series_id: str
    playbook_key: str
    period: str | None = None
    period_check_ok: bool = False
    resolved_period: str | None = None
    next_open_period: str | None = None


class DashboardResponse(BaseModel):
    """Shared response shape for GET /dashboard and the PUT/DELETE active and dismiss
    endpoints — the FE's single `["dashboard"]` query key.

    `published` = the tenant's workspace-wide published REPORT set
    (reports.dashboard_pinned_at IS NOT NULL), newest-published first — unchanged from
    before Task 4, entries byte-identical in shape.
    `published_series` = the tenant's tracking series (rolling-period Stage 1, Task 4)
    alongside the published reports, so the switcher can list both kinds (mock §5). A
    series has no separate "publish" gate of its own — existing IS selectable, the same
    way `compose_playbook_report(mode="tracking")` get-or-creates it.
    `active` = this user's displayed report: their stored selection (report OR the
    newest report in their selected series) if still available, else the most recently
    published report, else None.
    `active_is_fallback` = True ONLY when the user HAD a stored selection that is no
    longer available — an unpublished/deleted report, or a deleted series (tombstoned
    to NULL via `ON DELETE SET NULL`) — and the fallback substituted for it. A
    freshly-selected series with no report composed yet is NOT a fallback (`active` is
    None, `active_is_fallback` is False) — the selection itself is valid, it just
    hasn't produced anything to show.
    `active_tracking` = present iff `active` resolved from a currently-valid tracking
    selection (see `DashboardTrackingInfo`); None for a report selection, a fallback,
    or no selection at all.
    """

    published: list[ReportResponse]
    published_series: list[DashboardSeriesResponse] = Field(default_factory=list)
    active: ReportResponse | None = None
    active_is_fallback: bool = False
    active_tracking: DashboardTrackingInfo | None = None


class DashboardActiveRequest(BaseModel):
    """PUT /dashboard/active body. Accepts EITHER `report_id` (today's snapshot
    selection) OR `series_id` (rolling-period Stage 1, Task 4: a tracking selection) —
    exactly one must be set; both or neither is a 400 raised by the handler itself, not
    a pydantic 422 — a malformed id must still fail as a 404 (matching the reports
    API's no-existence-disclosure shape) rather than a validation error, so neither
    field is uuid-typed."""

    report_id: str | None = Field(default=None, min_length=1)
    series_id: str | None = Field(default=None, min_length=1)
