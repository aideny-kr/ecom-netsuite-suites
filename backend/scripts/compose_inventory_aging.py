"""Headless compose of the ``inventory_aging`` playbook (Slice 1, Task 6).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A6. ``main(tenant_id, locations, *, db) -> Report`` builds the real recipe
(``playbooks.build_playbook_recipe`` -- the same four ``bigquery_sql`` sources
Task 1's ``inventory_aging.build_sources`` produces, PLUS the "playbook" key that
makes it replayable), dispatches those sources, computes the ``AgingReport`` (Task
1's ``inventory_aging.compute``), renders it exactly like the mock (Task 2's
``build_inventory_aging_sections``/``render_report_html``), and persists it as a
``Report`` row titled "Inventory Aging Weekly" WITH that recipe (refresh-support
follow-up -- ``recipe_json`` is no longer left ``None``, see
``test_main_stores_the_real_recipe_so_refresh_and_auto_refresh_selector_work``),
``auto_refresh="off"`` (Slice 2's scheduled-jobs platform owns the run cadence, not
this report's own hourly/daily auto-refresh sweep) and ``dashboard_pinned_at`` set.

Why not ``playbooks.compose_playbook_report`` (which, as of the refresh-support
follow-up, DOES know how to compose inventory_aging via its rebuild hook): that
function has no ``auto_refresh``/``dashboard_pinned_at`` concept -- those are this
script's own §A6 requirements, not shared with the generic playbook-compose
endpoint. This script takes the same *shape* of steps compose_playbook_report takes
(build recipe -> dispatch sources -> compute/render -> persist -> audit -> commit)
but drives Task 1/2's own inventory_aging-specific functions directly so it can add
those two fields to the Report row that compose_playbook_report never sets.

``_fetch_payloads`` is a MODULE-LEVEL, monkeypatchable seam (not a ``main()``
parameter) -- same pattern as ``report_delivery.py``'s ``_build_drive_client``/
``_render_pdf_bytes``: the production implementation dispatches each source's
``bigquery_sql`` tool for real and converts its ``{"columns", "rows"}`` result (rows
are POSITIONAL lists -- see ``bigquery_service.execute_query``) into the
``list[dict]`` row shape ``inventory_aging.compute()`` reads; tests patch the whole
function with synthetic payloads (reusing Task 1's own fixture builder) so this
script's compose/persist steps are exercised without a live BigQuery round trip.

Usage:
    .venv/bin/python scripts/compose_inventory_aging.py --tenant <tenant-uuid> \\
        [--locations "Dimerco,Fedex,Panurgy"]

Run from ``backend/`` with this worktree's venv (see the module docstring pattern in
``scripts/render_statement_preview.py`` for why cwd matters: the venv's editable
install otherwise resolves ``app``/``scripts`` to the MAIN checkout, not this worktree).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.services.report.inventory_aging import (  # noqa: E402
    Source,
    compute,
    json_safe,
)
from app.services.report.playbooks import build_playbook_recipe  # noqa: E402
from app.services.report.report_html import (  # noqa: E402
    build_inventory_aging_provenance,
    build_inventory_aging_sections,
    inventory_aging_title,
    render_report_html,
)

# The Report ROW's series title (spec §A6): the app's page header and the Drive folder
# name. The rendered page's own <h1> is the mock's "Inventory Aging — Week of <date>"
# (spec §A1, `inventory_aging_title`) — the two are deliberately different strings.
TITLE = "Inventory Aging Weekly"


async def _fetch_payloads(
    db: AsyncSession,
    sources: dict[str, Source],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    correlation_id: str,
) -> dict[str, list[dict]]:
    """Production seam: dispatch every ``bigquery_sql`` source for real (no chat
    tool-call/audit bookkeeping — this is a headless system compose, not a chat turn)
    and convert each result's positional ``{"columns", "rows"}`` shape into the
    ``list[dict]`` rows ``inventory_aging.compute()`` reads (``row["location"]`` etc.).
    Re-establishes tenant RLS context before each dispatch — a tool call earlier in
    the loop may have committed (e.g. a connector token refresh), which clears the
    prior ``SET LOCAL`` (same reasoning as ``refresh_service._execute_sources``).

    Gate fix #8: fails closed with ``inventory_aging.SourceTruncated`` (naming the
    source) when a source's raw result reports ``truncated: true``
    (``bigquery_service.execute_query``'s own row-extraction cap silently dropped
    rows) — never silently passes a partial row set through to ``compute()``. The
    same check ``inventory_aging.rows_from_table_payload`` applies for the
    refresh/headless-compose-via-refresh-engine path; this script dispatches
    ``bigquery_sql`` directly rather than through that helper, so it needs its own
    check against the identical raw-result field."""
    from app.core.database import set_tenant_context
    from app.mcp.tools.bigquery_tools import bigquery_sql_execute
    from app.services.report.inventory_aging import SourceTruncated

    payloads: dict[str, list[dict]] = {}
    for rid, source in sources.items():
        await set_tenant_context(db, str(tenant_id))
        result = await bigquery_sql_execute(source["params"], {"db": db, "tenant_id": tenant_id})
        if not isinstance(result, dict) or result.get("error"):
            message = result.get("message") if isinstance(result, dict) else "malformed result"
            raise RuntimeError(f"source {rid} (bigquery_sql) failed: {message}")
        if result.get("truncated"):
            raise SourceTruncated(rid)
        columns = result.get("columns") or []
        rows = result.get("rows") or []
        payloads[rid] = [dict(zip(columns, row, strict=False)) for row in rows]
    return payloads


async def main(
    tenant_id: uuid.UUID,
    locations: list[str] | None,
    *,
    db: AsyncSession,
    actor_id: uuid.UUID | None = None,
    compare_days: int | None = None,
    trend_weeks: int | None = None,
):
    """Compose + persist one Inventory Aging Weekly report for ``tenant_id``.

    ``locations``: ``None`` uses ``inventory_aging.DEFAULT_LOCATIONS``
    (Dimerco/Fedex/Panurgy — spec §0 decision 1). ``db`` is required (not defaulted to
    an owned session) so a caller — a test with the ``db`` fixture, or a future
    scheduled-jobs executor step reusing an already-scoped session — always controls
    the transaction; the CLI entry point below opens and closes its own session
    around this call."""
    from app.core.database import set_tenant_context
    from app.models.report import Report
    from app.services import audit_service

    params: dict[str, Any] = {"locations": list(locations) if locations else None}
    if compare_days is not None:
        params["compare_days"] = compare_days
    if trend_weeks is not None:
        params["trend_weeks"] = trend_weeks

    # Refresh-support follow-up: build the RECIPE (not just its sources) via the
    # same build_playbook_recipe playbooks.compose_playbook_report/refresh_report
    # use — this is what gives the composed report a real, replayable recipe_json
    # (below) instead of the Task 6 workaround's ``None``. TITLE (this module's own
    # constant) and the recipe's own title are the identical string
    # ("Inventory Aging Weekly" — build_playbook_recipe's inventory_aging branch)
    # by construction, so using TITLE for the Report row's title column below stays
    # unchanged.
    _recipe_title, recipe = build_playbook_recipe("inventory_aging", params)
    sources = recipe["sources"]
    correlation_id = f"report-compose:inventory_aging:{uuid.uuid4().hex[:8]}"

    await set_tenant_context(db, str(tenant_id))
    payloads = await _fetch_payloads(db, sources, tenant_id=tenant_id, actor_id=actor_id, correlation_id=correlation_id)

    report_data = compute(payloads, params)
    # One timestamp for the head's "Composed …" line and the row's pin time, so the
    # page and the row never disagree about when this report was made.
    now = datetime.now(timezone.utc)
    sections = build_inventory_aging_sections(report_data, composed_at=now.isoformat())
    provenance = build_inventory_aging_provenance(report_data.provenance)
    page_title = inventory_aging_title(report_data)
    spec = {"title": page_title, "sections": sections}
    rendered_html = render_report_html(spec, provenance=provenance)
    spec_json = {"title": page_title, "sections": [json_safe(s) for s in sections]}

    # tool calls inside _fetch_payloads may commit (a connector token refresh) —
    # re-establish tenant context before the RLS-scoped insert below, same reasoning
    # as compose_playbook_report.
    await set_tenant_context(db, str(tenant_id))
    report = Report(
        tenant_id=tenant_id,
        title=TITLE,
        spec_json=spec_json,
        rendered_html=rendered_html,
        status="draft",
        created_by=actor_id,
        recipe_json=recipe,
        auto_refresh="off",
        dashboard_pinned_at=now,
    )
    db.add(report)
    await db.flush()

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.compose",
        actor_id=actor_id,
        actor_type="system",
        resource_type="report",
        resource_id=str(report.id),
        correlation_id=correlation_id,
        payload={"playbook": "inventory_aging", "source_count": len(sources)},
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    await db.refresh(report)
    return report


async def _run_cli(tenant_id: uuid.UUID, locations: list[str] | None) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.services.report.inventory_aging import SourceTruncated

    database_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            report = await main(tenant_id, locations, db=db)
            print(f"Composed report {report.id} ({report.title!r}) for tenant {tenant_id}")
    except SourceTruncated as exc:
        # Gate fix #8: a truncated BigQuery source must end this cron invocation
        # non-zero (never an unhandled traceback, and never a silent success) so
        # the scheduler correctly flags the run as FAILED — naming the source, per
        # the exception's own message.
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        await engine.dispose()


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="Tenant UUID")
    parser.add_argument(
        "--locations",
        default=None,
        help="Comma-separated stock locations (default: inventory_aging.DEFAULT_LOCATIONS)",
    )
    args = parser.parse_args()
    tenant_id = uuid.UUID(args.tenant)
    locations = [loc.strip() for loc in args.locations.split(",")] if args.locations else None
    asyncio.run(_run_cli(tenant_id, locations))


if __name__ == "__main__":
    _cli()
