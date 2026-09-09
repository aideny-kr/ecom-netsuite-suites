# backend/tests/test_scheduled_jobs_migration.py
"""Migration 100_scheduled_jobs — spec §B1 (binding): extends `schedules` with the
instruction/plan/approval/delivery/budget/catch-up columns a Scheduled Job needs.
Pattern: tests/test_report_migration.py (catalog-presence gate against the real
local Postgres, run through the `db` fixture's rollback-scoped transaction)."""

from sqlalchemy import text


async def test_schedules_table_has_the_scheduled_jobs_columns(db):
    cols = dict(
        (
            await db.execute(
                text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='schedules'")
            )
        ).all()
    )
    assert {
        # pre-existing (unchanged by this migration)
        "id",
        "tenant_id",
        "name",
        "schedule_type",
        "cron_expression",
        "is_active",
        "parameters",
        "created_at",
        "updated_at",
        # new, spec §B1
        "instruction",
        "plan_json",
        "plan_version",
        "plan_status",
        "pending_plan_json",
        "pending_plan_reason",
        "timezone",
        "delivery_json",
        "budget_json",
        "catch_up",
        "owner_id",
        "last_run_at",
        "last_run_status",
        "next_run_at",
        "paused_at",
        "pause_reason",
    } <= set(cols), f"schedules columns missing: {cols}"

    assert cols["instruction"] == "text"
    assert cols["plan_json"] == "jsonb"
    assert cols["plan_version"] == "integer"
    assert cols["plan_status"] == "text"
    assert cols["pending_plan_json"] == "jsonb"
    assert cols["pending_plan_reason"] == "text"
    assert cols["timezone"] == "text"
    assert cols["delivery_json"] == "jsonb"
    assert cols["budget_json"] == "jsonb"
    assert cols["catch_up"] == "text"
    assert cols["owner_id"] == "uuid"
    assert cols["last_run_at"] == "timestamp with time zone"
    assert cols["last_run_status"] == "text"
    assert cols["next_run_at"] == "timestamp with time zone"
    assert cols["paused_at"] == "timestamp with time zone"
    assert cols["pause_reason"] == "text"


async def test_schedules_new_columns_have_the_spec_defaults(db):
    row = (
        await db.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name='schedules' AND column_name='plan_version'"
            )
        )
    ).first()
    assert row is not None
    assert row[0] is not None and "0" in row[0]

    row = (
        await db.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='schedules' AND column_name='timezone'"
            )
        )
    ).first()
    assert row is not None and row[0] is not None and "UTC" in row[0]

    row = (
        await db.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='schedules' AND column_name='catch_up'"
            )
        )
    ).first()
    assert row is not None and row[0] is not None and "once" in row[0]


async def test_schedule_model_roundtrip_with_new_columns(db):
    """ORM insert + read under tenant context — proves the model's Mapped[]
    columns actually match the migrated table, not just the catalog."""
    from app.core.database import set_tenant_context
    from app.models.pipeline import Schedule
    from tests.conftest import create_test_tenant

    tenant = await create_test_tenant(db, name="JobsCorp")
    await set_tenant_context(db, str(tenant.id))

    schedule = Schedule(
        tenant_id=tenant.id,
        name="Inventory Aging Weekly",
        schedule_type="job",
        cron_expression="0 6 * * 1",
        instruction="Every Monday at 6am, compose the inventory aging report and deliver it to Drive.",
        plan_json={"steps": []},
        plan_status="draft",
        timezone="America/Los_Angeles",
    )
    db.add(schedule)
    await db.flush()

    assert schedule.id is not None
    assert schedule.plan_version == 0
    assert schedule.catch_up == "once"
    assert schedule.is_active is True
    assert schedule.pending_plan_json is None
    assert schedule.paused_at is None


# The up/down/up round-trip itself (spec §B1's binding requirement, brief's
# "migration up/down round-trips") is verified by actually running
# `alembic downgrade -1` then `alembic upgrade head` against the local verify DB
# — see the task report for the captured command + output. A pytest-level
# catalog check can only prove the FORWARD state (this file, above); alembic's
# versions/ modules are loaded by file path (numeric-prefixed filenames aren't
# valid dotted-import targets), so re-invoking downgrade() from inside a test
# would need alembic's own loader, not a real round-trip, and would only repeat
# the CLI check with more moving parts.
