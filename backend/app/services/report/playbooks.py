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

# NetSuite period name: "Jun 2026". NOT the SuiteQL injection boundary — it's a
# fail-fast pre-check so a malformed period 400s here instead of burning a tool round
# trip. netsuite_financial_report.build_period_filter independently re-validates every
# period token via its own _validate_period_name/_PERIOD_NAME_RE (a stricter real-month
# allowlist) before f-string-interpolating it into SQL, regardless of what reaches it
# from here — do not treat relaxing THIS regex alone as reopening an injection path, and
# do not relax build_period_filter's own check without parameterizing it instead.
_PERIOD_RE = re.compile(r"^[A-Z][a-z]{2} \d{4}$")

_MONTH_ABBRS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

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
    period = (params or {}).get("period", "").strip()
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


async def compose_playbook_report(db, *, playbook_key, params, tenant_id, actor_id):
    """Deterministic compose: recipe template → fail-closed source execution →
    frozen HTML → normal Report row. Reuses the refresh engine's execution seam
    on purpose — identical validation, identical failure semantics, and the
    resulting report auto-refreshes like any composed one."""
    from app.core.database import set_tenant_context
    from app.models.report import Report
    from app.services import audit_service
    from app.services.report.refresh_service import RefreshError, _execute_sources, _validated_sources
    from app.services.report.report_html import build_provenance, render_report_html
    from app.services.report.report_service import (
        assemble_spec,
        financial_statement_resolution_error,
        referenced_result_ids,
        required_result_ids,
        spec_json_safe,
    )

    title, recipe = build_playbook_recipe(playbook_key, params)
    correlation_id = f"report-playbook:{playbook_key}:{uuid.uuid4().hex[:8]}"

    await set_tenant_context(db, str(tenant_id))
    payloads = await _execute_sources(
        db,
        _validated_sources(recipe),
        referenced_result_ids(recipe["sections"]),
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type="user",
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
    report = Report(
        tenant_id=tenant_id,
        title=title,
        # Risk 3: a financial_statement model carries raw Decimal (spark/trend) fields —
        # sanitize BEFORE persisting (spec_json_safe), never before rendering (html
        # above was already built from the live Decimal-bearing spec).
        spec_json=spec_json_safe(spec),
        rendered_html=html,
        created_by=actor_id,
        recipe_json=recipe,
    )
    db.add(report)
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.compose",
        actor_id=actor_id,
        actor_type="user",
        resource_type="report",
        resource_id=str(report.id),
        correlation_id=correlation_id,
        payload={"playbook": playbook_key, "source_count": len(recipe["sources"])},
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    await db.refresh(report)
    return report
