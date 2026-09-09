"""Headless compose of the ``inventory_aging`` playbook (Slice 1, Task 6).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A6. ``main(tenant_id, locations, *, db) -> Report`` builds the four ``bigquery_sql``
sources (Task 1's ``inventory_aging.build_sources``), dispatches them, computes the
``AgingReport`` (Task 1's ``inventory_aging.compute``), renders it exactly like the
mock (Task 2's ``build_inventory_aging_sections``/``render_report_html``), and
persists it as a ``Report`` row titled "Inventory Aging Weekly" with
``auto_refresh="off"`` (Slice 2's scheduled-jobs platform owns the run cadence, not
this report's own hourly/daily auto-refresh sweep) and ``dashboard_pinned_at`` set.

Why not ``playbooks.compose_playbook_report``: that function fails CLOSED with a 501
for any playbook whose recipe's first section isn't ``financial_statement`` (Task 1's
own review-finding fix) -- inventory_aging's ``_INVENTORY_AGING_SECTION_TYPES`` never
is one (watch_items/kpi_cards/mid_row/bucket_table/top_positions/highlights/narrative),
so it can never reach that shared compose path. This script takes the same *shape* of
steps compose_playbook_report takes for a financial_statement (build recipe -> dispatch
sources -> compute/assemble -> render -> persist -> audit -> commit) but drives Task
1/2's own inventory_aging-specific functions directly instead of the shared
recipe-resolver machinery, which has no branch for these section types.

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
import dataclasses
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.services.report.inventory_aging import (  # noqa: E402
    Source,
    build_sources,
    compute,
)
from app.services.report.playbooks import build_playbook_recipe  # noqa: E402
from app.services.report.report_html import (  # noqa: E402
    build_inventory_aging_provenance,
    build_inventory_aging_sections,
    render_report_html,
)

TITLE = "Inventory Aging Weekly"


def _json_safe(value: Any) -> Any:
    """Recursively turn a value tree that may contain frozen dataclasses (Task 1's
    ``AgingReport`` and its nested ``BucketRow``/``TopItem``/``TrendPoint``/etc.),
    ``Decimal``, and ``date`` into something ``json.dumps`` — and therefore JSONB —
    can actually store. Never through ``float`` (no precision loss on money);
    ``Decimal`` becomes its exact string form, same convention as
    ``report_service.spec_json_safe``'s statement-model sanitizing."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


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
    prior ``SET LOCAL`` (same reasoning as ``refresh_service._execute_sources``)."""
    from app.core.database import set_tenant_context
    from app.mcp.tools.bigquery_tools import bigquery_sql_execute

    payloads: dict[str, list[dict]] = {}
    for rid, source in sources.items():
        await set_tenant_context(db, str(tenant_id))
        result = await bigquery_sql_execute(source["params"], {"db": db, "tenant_id": tenant_id})
        if not isinstance(result, dict) or result.get("error"):
            message = result.get("message") if isinstance(result, dict) else "malformed result"
            raise RuntimeError(f"source {rid} (bigquery_sql) failed: {message}")
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

    sources = build_sources(params)
    correlation_id = f"report-compose:inventory_aging:{uuid.uuid4().hex[:8]}"

    await set_tenant_context(db, str(tenant_id))
    payloads = await _fetch_payloads(db, sources, tenant_id=tenant_id, actor_id=actor_id, correlation_id=correlation_id)

    report_data = compute(payloads, params)
    sections = build_inventory_aging_sections(report_data)
    provenance = build_inventory_aging_provenance(report_data.provenance)
    spec = {"title": TITLE, "sections": sections}
    rendered_html = render_report_html(spec, provenance=provenance)
    spec_json = {"title": TITLE, "sections": [_json_safe(s) for s in sections]}

    # The replayable recipe (Task 1's registration) — inert while auto_refresh="off",
    # but capturing it now costs nothing and keeps this row consistent with every
    # other composed report's recipe_json contract for whenever refresh IS wired up
    # for this playbook.
    _, recipe = build_playbook_recipe("inventory_aging", params)

    # tool calls inside _fetch_payloads may commit (a connector token refresh) —
    # re-establish tenant context before the RLS-scoped insert below, same reasoning
    # as compose_playbook_report.
    await set_tenant_context(db, str(tenant_id))
    now = datetime.now(timezone.utc)
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

    database_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            report = await main(tenant_id, locations, db=db)
            print(f"Composed report {report.id} ({report.title!r}) for tenant {tenant_id}")
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
