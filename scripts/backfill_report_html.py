"""Re-render the frozen ``rendered_html`` of existing reports from their stored
``spec_json`` using the current renderer.

Why: report HTML is frozen at compose time (``report_service.compose_report``
writes ``rendered_html`` once). A renderer fix therefore only affects *new*
reports — rows composed before the fix keep their stale HTML. This one-off
backfill re-renders each report from its preserved ``spec_json`` so the fix
(e.g. GFM tables in narrative sections) reaches already-composed reports too.

Idempotent: a report whose HTML already matches a fresh render is left untouched
and is not counted as changed. Run with ``--dry-run`` first to see the count.

The ``reports`` table is FORCE-RLS (migration 084), so reads/writes require a
tenant context — pass one or more ``--tenant`` UUIDs (the script processes each
inside its own RLS-scoped transaction). Composition does not pass a per-tenant
accent today (``compose_report`` uses the renderer default), so the backfill
re-renders with the same default; if that ever changes, thread the accent here.

Run inside the deployed backend container (it has DATABASE_URL + the fixed code):

    docker exec ecom-netsuite-backend-1 python scripts/backfill_report_html.py \
        --tenant ce3dfaad-626f-4992-84e9-500c8291ca0a --dry-run
    docker exec ecom-netsuite-backend-1 python scripts/backfill_report_html.py \
        --tenant ce3dfaad-626f-4992-84e9-500c8291ca0a
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.database import async_session_factory, set_tenant_context
from app.models.report import Report
from app.services.report.report_html import render_report_html


async def backfill_tenant(tenant_id: str, dry_run: bool) -> tuple[int, int]:
    """Return (changed, total) for one tenant. SET LOCAL + queries + commit run
    in a single transaction so the RLS context stays in effect throughout."""
    changed = 0
    total = 0
    async with async_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            reports = (await session.execute(select(Report).where(Report.tenant_id == tenant_id))).scalars().all()
            total = len(reports)
            for report in reports:
                fresh_html = render_report_html(report.spec_json)
                if fresh_html != report.rendered_html:
                    changed += 1
                    if not dry_run:
                        report.rendered_html = fresh_html
            if dry_run:
                # Make no changes durable; the transaction is purely read.
                await session.rollback()
    return changed, total


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant",
        action="append",
        required=True,
        dest="tenants",
        metavar="UUID",
        help="Tenant UUID to backfill (repeatable). Required — reports is FORCE-RLS.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    args = parser.parse_args()

    grand_changed = 0
    grand_total = 0
    for tenant_id in args.tenants:
        changed, total = await backfill_tenant(tenant_id, args.dry_run)
        grand_changed += changed
        grand_total += total
        suffix = " (dry-run)" if args.dry_run else ""
        print(
            f"tenant {tenant_id}: re-rendered {changed}/{total} reports{suffix}",
            flush=True,
        )

    print(f"TOTAL: {grand_changed}/{grand_total} reports re-rendered", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
