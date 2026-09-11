"""Seed the **Inventory Aging Weekly** Scheduled Job (Slice 2, Task 8, spec §B7).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B7 (binding): "Create Inventory Aging Weekly for Framework from the mock's
instruction text; compile -> the five-step plan of the mock (queries -> compose
-> PDF + Excel -> Drive -> finish); approve; weekly Monday 06:00
America/Los_Angeles; delivery `Reports / Inventory aging`." This script does the
create + compile half only (the same shape ``POST /schedules`` takes for an
``instruction`` body — spec §B5 — driven headlessly instead of over HTTP,
mirroring ``scripts/compose_inventory_aging.py``'s CLI pattern): it leaves the
schedule ``plan_status = "pending_approval"``. Approving it, running it once, and
setting it live are a human's actions on the Scheduled jobs page (spec §B6) — the
brief's own checklist item for this task, done on staging after deploy, not by
this script.

INSTRUCTION/CRON_EXPRESSION/TIMEZONE/DELIVERY/BUDGET below are copied verbatim
from the approved mock (``scheduled-jobs-mock.html``'s "Inventory Aging Weekly"
job detail state) — the instruction text, the weekly Monday 06:00
America/Los_Angeles schedule, the `Reports / Inventory aging` Drive delivery, and
the "5 GB scanned · 10 min · $2 per run" budget. `NAME` is deliberately the exact
same string as ``compose_inventory_aging.TITLE`` — the Report row this job's
``report.compose``/``drive.upload`` steps produce each run — so the schedule row,
the report series, and the Drive folder pill on the page all read as the same job.

Idempotent (a "seed" verb, matching ``scripts/seed_drive_rag_flag.py``'s own
upsert convention): a rerun (staging redeploy, a second manual invocation) finds
the existing `schedule_type="job"` row by name and returns it unchanged rather
than compiling — and therefore paying for — a second LLM call and leaving a
duplicate row plus a duplicate approval waiting on the page.

Usage:
    .venv/bin/python scripts/seed_inventory_aging_job.py --tenant <tenant-uuid> \\
        [--owner <user-uuid>]

Run from ``backend/`` with this worktree's venv (see the module docstring
pattern in ``scripts/render_statement_preview.py`` for why cwd matters: the
venv's editable install otherwise resolves ``app``/``scripts`` to the MAIN
checkout, not this worktree).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.models.pipeline import Schedule  # noqa: E402
from app.services.jobs.compiler import Clarification, CompilerLLM, compile_instruction  # noqa: E402

# The Schedule row's `name` AND `compose_inventory_aging.TITLE` (the Report
# series it produces each run) are the identical string, deliberately — see
# the module docstring.
NAME = "Inventory Aging Weekly"

# Verbatim from the mock's Instruction panel, plus one appended sentence
# (live-run defect, brief G item 4): on staging the seed's compile asked
# whether to reuse an existing "Dimerco Inventory Aging Report" from an
# August chat instead of compiling -- a genuine clarifying question the mock's
# own text never answered, so the non-interactive seed created nothing. The
# appended sentence answers it explicitly so the compiler has no reason left
# to ask.
INSTRUCTION = (
    "Every Monday at 6am Pacific, build the inventory aging report for Dimerco, Fedex and "
    "Panurgy from the BigQuery inventory snapshot. Age each SKU by days since its last "
    "restock, bucket 0–30 / 31–60 / 61–90 / 91–180 / 180+, compare with the prior week "
    "and show the nine-week trend of aged share. Save a PDF of the report and an Excel "
    "workbook with every SKU per location to Google Drive under Reports / Inventory aging. "
    "If a run fails, retry once and then pause and tell me. "
    "Build a brand-new report each run with the inventory_aging playbook covering all three "
    "locations; do not refresh or extend any existing report."
)

# Verbatim from the mock's Schedule panel: weekly, Monday, 06:00 America/Los_Angeles.
CRON_EXPRESSION = "0 6 * * 1"
TIMEZONE = "America/Los_Angeles"

# Verbatim from the mock's Delivery panel: Drive folder + the in-app report series.
DELIVERY = {"drive": {"folder": "Reports / Inventory aging"}, "in_app": {"report_title": NAME}}

# Verbatim from the mock's Schedule panel: "5 GB scanned · 10 min · $2 per run"
# (`budget_json` spec §B1 shape: `{bytes_scanned, seconds, usd}`; 1 GB = 10**9
# bytes, matching `tests/jobs/test_executor.py`'s own "10 GB" convention).
BUDGET = {"bytes_scanned": 5_000_000_000, "seconds": 600, "usd": 2.0}


class NeedsClarificationError(RuntimeError):
    """Raised when the compiler cannot derive every required param from
    ``INSTRUCTION`` and asks a question instead (spec §B5: "creating
    NOTHING" — mirrors ``POST /schedules``' 409 path). The instruction above
    already names every location and behaviour the mock's compiled plan
    needs, so this should never fire in practice; it exists so a future edit
    to ``INSTRUCTION`` that accidentally drops a required detail fails loudly
    instead of silently seeding a job with no plan."""

    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


async def main(
    tenant_id: uuid.UUID,
    *,
    db: AsyncSession,
    owner_id: uuid.UUID | None = None,
    llm: CompilerLLM | None = None,
) -> Schedule:
    """Seed (or return the existing) Inventory Aging Weekly schedule for
    ``tenant_id``. ``db`` is required (not defaulted to an owned session) so a
    caller — a test with the ``db`` fixture, or this module's own CLI entry
    point below — always controls the transaction, same convention as
    ``compose_inventory_aging.main``.

    ``llm``: ``None`` (production default) resolves the tenant's real BYOK (or
    platform-default) provider inside ``compile_instruction`` — this script
    genuinely calls the compiler, per the brief ("compiles with the real
    compiler"). Tests inject a ``CompilerLLM`` wrapping a fake adapter so no
    network call happens."""
    from app.core.database import set_tenant_context
    from app.services import audit_service

    await set_tenant_context(db, str(tenant_id))

    existing = (
        await db.execute(
            select(Schedule).where(
                Schedule.tenant_id == tenant_id,
                Schedule.schedule_type == "job",
                Schedule.name == NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    compiled = await compile_instruction(
        db,
        tenant_id=tenant_id,
        instruction=INSTRUCTION,
        actor_id=owner_id,
        llm=llm,
        plan_version=0,
    )
    if isinstance(compiled, Clarification):
        raise NeedsClarificationError(compiled.question)

    # compile_instruction only flushes, never commits (its own docstring) — but
    # re-establish tenant context anyway before the insert below, the same
    # defensive convention compose_inventory_aging.main uses after a step that
    # may have committed on a shared session.
    await set_tenant_context(db, str(tenant_id))
    schedule = Schedule(
        tenant_id=tenant_id,
        name=NAME,
        schedule_type="job",
        cron_expression=CRON_EXPRESSION,
        timezone=TIMEZONE,
        is_active=True,
        instruction=INSTRUCTION,
        plan_json=compiled.plan_json,
        plan_version=0,
        plan_status="pending_approval",
        delivery_json=DELIVERY,
        budget_json=BUDGET,
        owner_id=owner_id,
        created_via="seed",
    )
    db.add(schedule)
    await db.flush()

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="schedule",
        action="schedule.create",
        actor_id=owner_id,
        actor_type="system",
        resource_type="schedule",
        resource_id=str(schedule.id),
        payload={
            "instruction": INSTRUCTION,
            "plan_status": "pending_approval",
            "model": compiled.model,
            "seed": "inventory_aging_weekly",
        },
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    await db.refresh(schedule)
    return schedule


async def _run_cli(tenant_id: uuid.UUID, owner_id: uuid.UUID | None) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings

    database_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            schedule = await main(tenant_id, db=db, owner_id=owner_id)
            print(
                f"Schedule {schedule.id} ({schedule.name!r}) plan_status={schedule.plan_status!r} "
                f"for tenant {tenant_id} — approve it on the Scheduled jobs page."
            )
    except NeedsClarificationError as exc:
        print(f"ERROR: the compiler needs clarification and created nothing: {exc.question}", file=sys.stderr)
        sys.exit(1)
    finally:
        await engine.dispose()


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="Tenant UUID")
    parser.add_argument("--owner", default=None, help="Owner user UUID (schedule owner + compile actor)")
    args = parser.parse_args()
    tenant_id = uuid.UUID(args.tenant)
    owner_id = uuid.UUID(args.owner) if args.owner else None
    asyncio.run(_run_cli(tenant_id, owner_id))


if __name__ == "__main__":
    _cli()
