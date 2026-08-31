"""Playbook catalog — curated deterministic report recipes (no LLM in the loop).

Each playbook maps 1:1 to a netsuite_financial_report REPORT_TEMPLATE, so every
number is a statement-grade GL aggregate. build_playbook_recipe emits exactly
the recipe_json schema the refresh engine validates, which is what buys playbook
reports auto-refresh, versioning, and download with zero extra machinery.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from app.services.report.period_resolver import PeriodUnavailableReason

# NetSuite period name: "Jun 2026". NOT the SuiteQL injection boundary — it's a
# fail-fast pre-check so a malformed period 400s here instead of burning a tool round
# trip. netsuite_financial_report.build_period_filter independently re-validates every
# period token via its own _validate_period_name/_PERIOD_NAME_RE (a stricter real-month
# allowlist) before f-string-interpolating it into SQL, regardless of what reaches it
# from here — do not treat relaxing THIS regex alone as reopening an injection path, and
# do not relax build_period_filter's own check without parameterizing it instead.
_PERIOD_RE = re.compile(r"^[A-Z][a-z]{2} \d{4}$")

_MONTH_ABBRS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_FULL = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip

# How many trailing months feed an income_statement's trend comparison source (r4).
_TREND_MONTHS = 6


def _parse_period(period: str) -> tuple[int, int]:
    """Validated "Mon YYYY" -> (month 1-12, year). ``ValueError`` on malformed input —
    reuses ``_PERIOD_RE`` (the same fail-fast pre-check ``build_playbook_recipe``
    applies) and additionally rejects a regex-shaped but non-real month ("Xxx 2026")."""
    if not isinstance(period, str) or not _PERIOD_RE.match(period):
        raise ValueError("period must be a NetSuite period name like 'Jun 2026'")
    month_str, year_str = period.split(" ")
    try:
        month = _MONTH_ABBRS.index(month_str) + 1
    except ValueError:
        raise ValueError(
            f"period must be a NetSuite period name like 'Jun 2026' (unknown month '{month_str}')"
        ) from None
    return month, int(year_str)


def _format_period(month: int, year: int) -> str:
    return f"{_MONTH_ABBRS[month - 1]} {year}"


# Human period spellings normalize_period accepts, each mapped to a (month, year)
# extraction it then hands to _format_period for the canonical "Mon YYYY" render.
_PERIOD_NAME_RE = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")  # "jun 2026" / "June 2026"
_PERIOD_NUMERIC_RE = re.compile(r"^(\d{1,2})/(\d{4})$")  # "6/2026" / "06/2026"
_PERIOD_ISO_RE = re.compile(r"^(\d{4})-(\d{2})$")  # "2026-06"

_MONTH_NAME_TO_NUM: dict[str, int] = {}
for _i, (_abbr, _full) in enumerate(zip(_MONTH_ABBRS, _MONTH_FULL), start=1):
    _MONTH_NAME_TO_NUM[_abbr.lower()] = _i
    _MONTH_NAME_TO_NUM[_full.lower()] = _i
del _i, _abbr, _full


def normalize_period(raw: str) -> str:
    """Accept several unambiguous human period spellings and return the canonical
    NetSuite "Mon YYYY" form ``build_playbook_recipe``/``_PERIOD_RE`` expect. Pure
    string mapping -- no LLM, no calendar guessing beyond these exact forms (all
    case-insensitive, whitespace-tolerant around the whole string and between the
    month/year tokens):
      - 3-letter month abbreviation: "jun 2026", "JUN 2026" -> "Jun 2026"
      - full English month name: "June 2026", "june 2026" -> "Jun 2026"
      - numeric month/year, 1 or 2 digit month: "6/2026", "06/2026" -> "Jun 2026"
      - ISO month: "2026-06" -> "Jun 2026"
    The already-canonical form round-trips unchanged. Raises ``ValueError`` (kept SHORT
    -- it renders inline in the report launcher) for anything else, including a
    regex-shaped but out-of-range month (e.g. "13/2026") or a 2-digit year."""
    accepted = "period must look like 'Jun 2026', 'June 2026', '6/2026', or '2026-06'"
    if not isinstance(raw, str):
        raise ValueError(accepted)
    candidate = raw.strip()

    match = _PERIOD_NAME_RE.match(candidate)
    if match:
        month = _MONTH_NAME_TO_NUM.get(match.group(1).lower())
        if month is not None:
            return _format_period(month, int(match.group(2)))

    match = _PERIOD_NUMERIC_RE.match(candidate)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return _format_period(month, year)

    match = _PERIOD_ISO_RE.match(candidate)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return _format_period(month, year)

    raise ValueError(accepted)


def prior_period(period: str) -> str:
    """One calendar month back: ``"Jun 2026" -> "May 2026"``; crosses the year boundary
    (``"Jan 2026" -> "Dec 2025"``)."""
    month, year = _parse_period(period)
    if month == 1:
        return _format_period(12, year - 1)
    return _format_period(month - 1, year)


def yoy_period(period: str) -> str:
    """Same month, one year back: ``"Jun 2026" -> "Jun 2025"``."""
    month, year = _parse_period(period)
    return _format_period(month, year - 1)


def trailing_periods(period: str, count: int) -> str:
    """``count`` consecutive months ending at (and including) ``period``, chronological
    (oldest first), comma-joined: ``trailing_periods("Jun 2026", 6) ->
    "Jan 2026,Feb 2026,Mar 2026,Apr 2026,May 2026,Jun 2026"``."""
    _parse_period(period)  # validate up front — count=1 never reaches prior_period below
    periods = [period]
    for _ in range(count - 1):
        periods.append(prior_period(periods[-1]))
    return ",".join(reversed(periods))


class PeriodUnavailableError(ValueError):
    """``mode="tracking"`` compose (Task 3, rolling-period Stage 1) could not resolve a
    closed period to compose against. A ``ValueError`` subclass on purpose: the
    endpoint's existing ``except ValueError`` -> 400 branch handles it with zero
    endpoint-level changes, and ``str(error)`` is already the operator-facing message
    that reaches the launcher UI. ``.reason`` carries the underlying
    ``PeriodUnavailableReason`` for callers that want to distinguish WHY without
    parsing the message string.
    """

    # Written for the operator viewing the launcher, not a developer reading logs —
    # never leak the bare enum value (see period_resolver.PeriodUnavailableReason).
    _MESSAGES: dict[PeriodUnavailableReason, str] = {
        PeriodUnavailableReason.NO_CLOSED_PERIOD: (
            "NetSuite doesn't have a closed accounting period yet — there's nothing to track."
        ),
        PeriodUnavailableReason.UNSUPPORTED_PERIOD_NAME: (
            "The last closed period's name doesn't match NetSuite's standard 'Mon YYYY' "
            "format, so it can't be tracked automatically yet."
        ),
        PeriodUnavailableReason.UNREACHABLE: (
            "Couldn't reach NetSuite to check which period is closed — try again shortly."
        ),
    }

    def __init__(self, reason: PeriodUnavailableReason):
        self.reason = reason
        message = self._MESSAGES.get(reason, "Couldn't determine the last closed accounting period.")
        super().__init__(message)


PLAYBOOKS: dict[str, dict] = {
    "income_statement": {
        "name": "Income Statement",
        "description": "Statement-grade P&L for one accounting period, straight from the GL.",
        "params": [{"key": "period", "label": "Accounting period", "example": "Jun 2026"}],
    },
    "balance_sheet": {
        "name": "Balance Sheet",
        "description": "Balance Sheet as of the end of an accounting period (inception-to-date).",
        "params": [{"key": "period", "label": "As-of period", "example": "Jun 2026"}],
    },
    "trial_balance": {
        "name": "Trial Balance",
        "description": "All GL accounts with debit/credit totals for one accounting period.",
        "params": [{"key": "period", "label": "Accounting period", "example": "Jun 2026"}],
    },
}


def _source(report_type: str, period: str) -> dict:
    return {
        "tool": "netsuite_financial_report",
        "params": {"report_type": report_type, "period": period},
        "connection_id": None,
    }


def build_playbook_recipe(playbook_key: str, params: dict[str, str]) -> tuple[str, dict]:
    meta = PLAYBOOKS.get(playbook_key)
    if meta is None:
        raise ValueError(f"Unknown playbook: '{playbook_key}'")
    # normalize_period accepts several human spellings ("jun 2026", "June 2026",
    # "6/2026", "2026-06") and returns the canonical "Mon YYYY" form -- everything
    # below (title, sections, EVERY source's params) uses that canonical string, so
    # SuiteQL always receives exactly "Jun 2026" regardless of what the operator typed.
    period = normalize_period((params or {}).get("period", ""))
    # Redundant-by-construction (normalize_period's output is always canonical) but kept
    # as an explicit belt-and-suspenders gate -- never trust a helper's own invariant
    # blindly at a boundary this close to SQL interpolation (see _PERIOD_RE's docstring).
    if not _PERIOD_RE.match(period):
        raise ValueError("period must be a NetSuite period name like 'Jun 2026'")
    title = f"{meta['name']} — {period}"

    # Every statement gets prior-period comparison (r2); income_statement additionally
    # gets same-month-last-year (r3) and a trailing-trend source (r4) — balance_sheet
    # and trial_balance are point-in-time/period snapshots without a v1 trend view.
    sources = {"r1": _source(playbook_key, period), "r2": _source(playbook_key, prior_period(period))}
    compare = {"prior": "r2"}
    if playbook_key == "income_statement":
        sources["r3"] = _source(playbook_key, yoy_period(period))
        sources["r4"] = _source("income_statement_trend", trailing_periods(period, _TREND_MONTHS))
        compare["yoy"] = "r3"
        compare["trend"] = "r4"

    recipe = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        # No heading section: render_report_html already emits an outer <h1> from
        # assemble_spec's title — a recipe-authored heading here duplicated it
        # back-to-back in the rendered HTML.
        "sections": [
            {
                "type": "financial_statement",
                "result_id": "r1",
                "statement": playbook_key,
                "period": period,
                "compare": compare,
            }
        ],
        "sources": sources,
    }
    return title, recipe


async def compose_playbook_report(
    db,
    *,
    playbook_key,
    params,
    tenant_id,
    actor_id,
    mode="period",
    actor_type="user",
    closed_period=None,
):
    """Deterministic compose: recipe template → fail-closed source execution →
    frozen HTML → normal Report row. Reuses the refresh engine's execution seam
    on purpose — identical validation, identical failure semantics, and the
    resulting report auto-refreshes like any composed one.

    ``actor_type`` defaults to "user" because the HTTP endpoint (a real person) was the
    only caller for Stage 1. Stage 2's scheduled sweep passes "system" with
    ``actor_id=None``: a scheduled compose has no person behind it, and an audit event
    claiming one is a false record. ``refresh_service.refresh_report`` threads the same
    pair for the same reason.

    ``mode="period"`` (default): exactly the pre-Task-3 behaviour — ``params["period"]``
    is whatever the caller typed. ``mode="tracking"`` (rolling-period Stage 1, Task 3):
    the period is resolved server-side from NetSuite's own close state instead —
    ``params["period"]`` is ignored — and the resulting report links into a per-tenant,
    per-playbook ``ReportSeries`` (get-or-created) via ``series_id``. Composing tracking
    twice for the same already-covered period is a no-op that returns the existing
    report: a series+period pair is looked up deliberately BEFORE doing any work, never
    inferred from catching the partial unique index's IntegrityError (that index is a
    backstop invariant, not the control-flow mechanism)."""
    from sqlalchemy import select, text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.core.database import set_tenant_context
    from app.models.report import Report
    from app.models.report_series import ReportSeries
    from app.services import audit_service
    from app.services.report.period_resolver import resolve_last_closed_period
    from app.services.report.refresh_service import RefreshError, _execute_sources, _validated_sources
    from app.services.report.report_html import build_provenance, render_report_html
    from app.services.report.report_service import (
        assemble_spec,
        financial_statement_resolution_error,
        referenced_result_ids,
        required_result_ids,
        spec_json_safe,
    )

    if mode not in ("period", "tracking"):
        raise ValueError(f"mode must be 'period' or 'tracking' (got {mode!r})")

    series_id: uuid.UUID | None = None
    if mode == "tracking":
        # `closed_period` lets a caller that ALREADY resolved this tenant's period hand
        # it in (T2 gate round 1: the Stage 2 sweep resolves once per tenant, but every
        # per-series compose re-resolved the same answer at 2 NetSuite round trips a
        # time, silently undoing that saving). Injecting beats switching this call to
        # the 300s-cached resolver: that would also make the INTERACTIVE path serve a
        # stale period for up to 5 minutes after a close, which is exactly when a user
        # clicks compose. The scheduled caller opts into reuse; a person always gets
        # a live answer.
        closed = closed_period or await resolve_last_closed_period(db, tenant_id)
        if not closed.resolved:
            raise PeriodUnavailableError(closed.reason)
        # Tracking mode resolves the period server-side — anything the caller typed
        # in params["period"] is ignored, never blended with the resolved value.
        params = {"period": closed.name}

        # report_series / reports are FORCE-RLS'd — establish tenant context before
        # touching either.
        await set_tenant_context(db, str(tenant_id))

        # T2-gate MAJOR B (round 2): the series row used to be get-or-created HERE,
        # before _execute_sources ran below. A tool call inside _execute_sources can
        # commit the session mid-flight (an OAuth token refresh — see the "tool calls
        # may commit" comment further down); if compose then failed (a required rid's
        # RefreshError, or the financial_statement_resolution_error 502 path), that
        # mid-flight commit durably persisted the series INSERT even though the
        # exception propagated before any Report row or report.compose audit event
        # ever existed — a phantom series with zero reports, surfaced by GET
        # /dashboard's published_series as trackable, that the user never successfully
        # created.
        #
        # Fix: only ever LOOK UP (never create) the series here — a read-only SELECT
        # can't orphan anything. The real get-or-create INSERT moves to right before
        # the Report row, once _execute_sources has actually succeeded (see below).
        # This lookup still buys the idempotency short-circuit (composing tracking
        # twice for an already-covered period skips re-dispatching every source) for
        # every case except "no series exists yet at all" — which by definition has no
        # existing report to be idempotent about.
        existing_series_id = (
            await db.execute(
                select(ReportSeries.id).where(
                    ReportSeries.tenant_id == tenant_id,
                    ReportSeries.playbook_key == playbook_key,
                )
            )
        ).scalar_one_or_none()

        if existing_series_id is not None:
            existing = (
                await db.execute(
                    # tenant_id is NOT redundant beside series_id: reports.series_id is a
                    # plain single-column FK (migration 093), not tenant-composite, so
                    # nothing at the schema level stops a row of another tenant pointing
                    # at this series. Without this predicate the only thing standing
                    # between that row and a cross-tenant read is RLS -- make the query
                    # itself the guard. Same predicate on the conflict-resolution lookup
                    # below; applying it to one and not its sibling is the exact shape
                    # that produced rounds 3 and 4's findings.
                    select(Report).where(
                        Report.tenant_id == tenant_id,
                        Report.series_id == existing_series_id,
                        Report.period == closed.name,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            # A series already exists (an earlier period's compose created it) but not
            # for THIS period yet — carry its id forward so the post-execute block
            # below never needs to touch report_series again.
            series_id = existing_series_id

    title, recipe = build_playbook_recipe(playbook_key, params)
    period = recipe["sections"][0]["period"]
    correlation_id = f"report-playbook:{playbook_key}:{uuid.uuid4().hex[:8]}"

    await set_tenant_context(db, str(tenant_id))
    payloads = await _execute_sources(
        db,
        _validated_sources(recipe),
        referenced_result_ids(recipe["sections"]),
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type=actor_type,
        correlation_id=correlation_id,
        # Risk 2 (statement compare-degrade seam): only the CURRENT-period source (r1)
        # is a hard dependency for a financial_statement recipe — a prior/yoy/trend
        # source outage renders the statement without that comparison instead of
        # failing the whole compose. See report_service.required_result_ids /
        # refresh_service._execute_sources docstrings for the mechanics.
        required_rids=required_result_ids(recipe["sections"]),
    )
    spec = assemble_spec(title, recipe["sections"], lambda rid: payloads[rid])
    # T2 gate M2: r1 can RESOLVE but still fail to become a real statement (e.g. a
    # well-shaped but empty account list — statement_builder._require_rows rejects
    # that). For a statement report the section IS the report, so this fails closed
    # (never persists a Report row) rather than letting the error-card degrade publish
    # a contentless statement the way any OTHER section type's failure would.
    error_reason = financial_statement_resolution_error(recipe["sections"], spec)
    if error_reason is not None:
        raise RefreshError(502, f"statement could not be built: {error_reason}")
    html = render_report_html(
        spec,
        freshness={"composed_at": recipe["captured_at"], "refreshed_at": ""},
        # T2 gate M1: resolved_rids marks any compare rid the degrade seam omitted from
        # payloads as "not available this run" in the frozen provenance block instead of
        # falsely claiming it executed — see build_provenance's docstring.
        provenance=build_provenance(recipe["sources"], recipe["captured_at"], resolved_rids=set(payloads)),
    )

    # tool calls may commit (e.g. token refresh) — re-establish before RLS writes
    await set_tenant_context(db, str(tenant_id))

    if mode == "tracking" and series_id is None:
        # Only reached the FIRST time this (tenant, playbook_key) is ever tracked — the
        # lookup above found no existing row. Durably create it now, for the first
        # time, only after compose has actually succeeded (see the MAJOR B comment
        # above for why this moved from before _execute_sources to here). Still a real
        # Postgres upsert (ON CONFLICT DO NOTHING + re-select), not a bare INSERT: two
        # concurrent FIRST-ever tracking composes for the same (tenant, playbook_key)
        # can both reach this point with series_id=None, and the loser must resolve to
        # the winner's row rather than violate uq_report_series_tenant_playbook as an
        # unhandled IntegrityError -> 500 (compose_playbook_endpoint only catches
        # ValueError/RefreshError).
        insert_stmt = (
            pg_insert(ReportSeries)
            .values(tenant_id=tenant_id, playbook_key=playbook_key, created_by=actor_id)
            .on_conflict_do_nothing(constraint="uq_report_series_tenant_playbook")
            .returning(ReportSeries.id)
        )
        series_id = (await db.execute(insert_stmt)).scalar_one_or_none()
        if series_id is None:
            # DO NOTHING means our insert didn't happen — a concurrent compose won the
            # race. Re-select the winner's row: this is the ordinary get-or-create
            # outcome of losing a benign race, not an error.
            series_id = (
                await db.execute(
                    select(ReportSeries.id).where(
                        ReportSeries.tenant_id == tenant_id,
                        ReportSeries.playbook_key == playbook_key,
                    )
                )
            ).scalar_one()

    # MAJOR 1 (T2 gate, round 3): moving this Report creation to after
    # _execute_sources (to stop orphaned series — see the ReportSeries get-or-create
    # comment above) widened the window between the pre-flight "does a report already
    # exist for this (series_id, period)?" SELECT further up and this insert —
    # _execute_sources is a full round of live NetSuite calls in between. Two
    # concurrent mode="tracking" composes for the same tenant/playbook/period
    # (double-click, retry-on-timeout, two tabs) both pass the pre-flight check, both
    # reach here, and the second would violate the partial unique index
    # uq_reports_series_id_period as an unhandled IntegrityError -> 500
    # (compose_playbook_endpoint only catches ValueError/RefreshError). Same fix as
    # the ReportSeries insert right above: a real Postgres upsert (ON CONFLICT DO
    # NOTHING + re-select), not a bare INSERT — the loser resolves to the winner's row,
    # which is the correct semantic (a concurrent compose already produced this
    # period's report), rather than an unhandled error.
    #
    # The index is a partial one (WHERE series_id IS NOT NULL AND period IS NOT NULL —
    # see migration 093_report_series.py), so it never applies to a mode="period"
    # report (series_id is always None there): index_where must match that predicate
    # text verbatim for Postgres to use it as the ON CONFLICT arbiter, and a period-
    # mode row simply never satisfies it, so this insert always proceeds normally for
    # that mode — no special-casing needed.
    insert_stmt = (
        pg_insert(Report)
        .values(
            tenant_id=tenant_id,
            title=title,
            # Risk 3: a financial_statement model carries raw Decimal (spark/trend)
            # fields — sanitize BEFORE persisting (spec_json_safe), never before
            # rendering (html above was already built from the live Decimal-bearing
            # spec).
            spec_json=spec_json_safe(spec),
            rendered_html=html,
            created_by=actor_id,
            recipe_json=recipe,
            # Rolling-period Stage 1 (Task 3): period is set in BOTH modes (the
            # canonical "Mon YYYY" this report covers); series_id only for a tracking
            # compose — a mode="period" report stays a one-off snapshot, not linked
            # into any lineage.
            period=period,
            series_id=series_id,
        )
        .on_conflict_do_nothing(
            index_elements=[Report.series_id, Report.period],
            index_where=text("series_id IS NOT NULL AND period IS NOT NULL"),
        )
        .returning(Report.id)
    )
    report_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    if report_id is None:
        # DO NOTHING means our insert didn't happen — a concurrent tracking compose
        # already produced this exact (series, period) report. Resolve to its row and
        # return early: there is nothing of ours to audit-log or commit, exactly like
        # the pre-flight "already covered" early return further up.
        report = (
            await db.execute(
                select(Report).where(
                    Report.tenant_id == tenant_id,
                    Report.series_id == series_id,
                    Report.period == period,
                )
            )
        ).scalar_one_or_none()
        if report is None:
            # We lost the ON CONFLICT race, but the row that beat us is not visible to
            # THIS tenant. uq_reports_series_id_period is keyed on (series_id, period)
            # with no tenant column, so the only way here is corrupt data: a report of
            # another tenant pointing at this tenant's series (reports.series_id is a
            # plain FK, not tenant-composite -- see migration 093). Fail loudly and
            # legibly rather than fall through: a bare scalar_one() would raise
            # NoResultFound as an opaque 500, and returning the other tenant's row
            # would be a cross-tenant read. ValueError is what compose_playbook_endpoint
            # already maps to a clean client error.
            raise ValueError(
                f"Report series {series_id} already has a {period!r} report owned by a "
                "different tenant; refusing to read across tenants"
            )
        return report

    report = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one()
    audit_payload = {"playbook": playbook_key, "source_count": len(recipe["sources"])}
    if series_id is not None:
        audit_payload["series_id"] = str(series_id)
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.compose",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="report",
        resource_id=str(report.id),
        correlation_id=correlation_id,
        payload=audit_payload,
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    await db.refresh(report)
    return report
