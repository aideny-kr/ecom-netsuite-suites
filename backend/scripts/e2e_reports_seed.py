"""Seed two reports for the reports Playwright E2E (frontend/e2e/reports.spec.ts).

Runs INSIDE the local docker backend container (so it shares the container's
DB connection + the report renderer), e.g.

    docker exec ecom-netsuite-suites-backend-1 \
        python scripts/e2e_reports_seed.py <tenant_id> <created_by_user_id> [unique_suffix]

The optional <unique_suffix> is appended to both report titles so a single
spec run can target ITS OWN rows unambiguously. This matters because the local
docker Postgres connects as the `postgres` SUPERUSER, which BYPASSES RLS even
with FORCE ROW LEVEL SECURITY enabled — so the list endpoint returns rows for
ALL tenants locally (the authoritative cross-tenant isolation proof is the
post-deploy live smoke against uat-smoke, per the plan Task 15, NOT this spec).
A unique title makes the golden-path assertions deterministic regardless.

Inserts, under the given tenant's RLS context:
  1. A "golden-path" report whose rendered HTML carries a known <h1> heading
     plus a server-rendered chart <svg> (the iframe-render assertion).
  2. A second report whose spec contains an `error` section, so the renderer
     emits the "Data unavailable:" error block (the does-not-crash assertion).

Prints a single JSON line to stdout so the spec can parse the two report ids:
  {"chart_report_id": "...", "error_report_id": "...",
   "chart_heading": "...", "error_reason": "..."}

Exit codes:
  0 — both rows inserted, ids printed
  2 — could not reach DB / insert failed
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import set_tenant_context
from app.schemas.chart import ChartAxis, ChartData
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_html import render_report_html

CHART_HEADING_BASE = "Q2 Revenue Review E2E"
ERROR_TITLE_BASE = "Q3 Report With Missing Data E2E"
ERROR_REASON = "missing_result: r_does_not_exist (E2E error section)"


def _build_chart_spec(heading: str) -> dict:
    chart = ChartData(
        chart_type="bar",
        title="Quarterly Revenue",
        x_axis=ChartAxis(label="Period", key="period"),
        y_axes=[ChartAxis(label="Revenue", key="revenue", color="#6366f1")],
        data=[
            {"period": "Q1", "revenue": 100},
            {"period": "Q2", "revenue": 150},
        ],
    )
    return {
        # render_report_html always emits the title as the top-level <h1>, so we
        # do NOT add a redundant `heading` section (it would produce two
        # identical <h1>s → a Playwright strict-mode violation).
        "title": heading,
        "sections": [
            {"type": "narrative", "markdown": "Revenue grew **50%** quarter over quarter."},
            {"type": "chart", "svg": render_chart_svg(chart), "chart_type": "bar"},
            {
                "type": "table",
                "columns": ["Period", "Revenue"],
                "rows": [["Q1", "100"], ["Q2", "150"]],
                "row_count": 2,
            },
        ],
        "provenance": {"sources": ["metric:revenue@v1"]},
    }


def _build_error_spec(title: str) -> dict:
    return {
        # No redundant `heading` section — the title is rendered as <h1> already.
        "title": title,
        "sections": [
            {"type": "narrative", "markdown": "This section's data could not be resolved."},
            {"type": "error", "reason": ERROR_REASON},
        ],
        "provenance": {"sources": []},
    }


async def _insert(conn, tenant_id: str, created_by: str | None, spec: dict) -> str:
    html = render_report_html(spec, accent_hsl="142 70% 45%")
    new_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO reports "
            "(id, tenant_id, title, spec_json, rendered_html, status, created_by, version) "
            "VALUES (:id, :tenant_id, :title, CAST(:spec AS jsonb), :html, 'draft', :created_by, 1)"
        ),
        {
            "id": new_id,
            "tenant_id": tenant_id,
            "title": spec["title"],
            "spec": json.dumps(spec),
            "html": html,
            "created_by": created_by,
        },
    )
    return new_id


async def seed(tenant_id: str, created_by: str | None, suffix: str) -> int:
    chart_heading = f"{CHART_HEADING_BASE} {suffix}".strip()
    error_title = f"{ERROR_TITLE_BASE} {suffix}".strip()
    engine = create_async_engine(
        settings.DATABASE_URL_DIRECT or settings.DATABASE_URL,
        echo=False,
    )
    try:
        async with engine.begin() as conn:
            # RLS WITH CHECK requires the tenant context to match the inserted
            # tenant_id, exactly like the TOOL compose path (report_service).
            await set_tenant_context(conn, tenant_id)
            # Mark onboarding complete so the (dashboard) layout doesn't auto-start
            # the onboarding chat — that hook 502s on a stack without the LLM and
            # would pollute the spec's "no console errors" assertion. A
            # reports-viewer is a realistic post-onboarding user anyway.
            await conn.execute(
                text(
                    "UPDATE tenant_configs SET onboarding_completed_at = now() "
                    "WHERE tenant_id = :tid AND onboarding_completed_at IS NULL"
                ),
                {"tid": tenant_id},
            )
            chart_id = await _insert(conn, tenant_id, created_by, _build_chart_spec(chart_heading))
            error_id = await _insert(conn, tenant_id, created_by, _build_error_spec(error_title))
    except Exception as exc:  # noqa: BLE001 — script: surface the failure + exit 2
        print(f"seed failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()

    print(
        json.dumps(
            {
                "chart_report_id": chart_id,
                "error_report_id": error_id,
                "chart_heading": chart_heading,
                "error_title": error_title,
                "error_reason": ERROR_REASON,
            }
        )
    )
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: e2e_reports_seed.py <tenant_id> [created_by_user_id] [unique_suffix]",
            file=sys.stderr,
        )
        return 2
    tenant_id = sys.argv[1]
    created_by = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    suffix = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else ""
    return asyncio.run(seed(tenant_id, created_by, suffix))


if __name__ == "__main__":
    sys.exit(main())
