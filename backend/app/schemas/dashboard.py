from pydantic import BaseModel, Field

from app.schemas.report import ReportResponse


class DashboardResponse(BaseModel):
    """Shared response shape for GET /dashboard and the PUT/DELETE active
    endpoints — the FE's single `["dashboard"]` query key.

    `published` = the tenant's workspace-wide published set
    (reports.dashboard_pinned_at IS NOT NULL), newest-published first.
    `active` = this user's displayed report: their stored selection if it is
    still published, else the most recently published report, else None.
    `active_is_fallback` = True ONLY when the user HAD a stored selection that
    is no longer available (unpublished — a delete cascades the preference row
    away entirely, see UserDashboardPreference, so it degrades to "never
    chosen" instead) and the fallback substituted for it. False when the user
    simply never chose, or when their choice is still valid.
    """

    published: list[ReportResponse]
    active: ReportResponse | None = None
    active_is_fallback: bool = False


class DashboardActiveRequest(BaseModel):
    """PUT /dashboard/active body. `report_id` is a plain str, not uuid.UUID —
    a malformed value must fail inside the handler as a 404 (matching the
    reports API's no-existence-disclosure shape), not as pydantic's automatic
    422 that a UUID-typed field would trigger."""

    report_id: str = Field(min_length=1)
