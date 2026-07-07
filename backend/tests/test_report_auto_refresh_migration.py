# backend/tests/test_report_auto_refresh_migration.py
from sqlalchemy import text

from app.core.database import set_tenant_context
from app.models.report import Report
from tests.conftest import create_test_tenant  # pattern: test_report_migration.py


async def test_reports_auto_refresh_column_text_not_null_default_daily(db):
    """Slice C (spec §4C/§6.1): the user-chosen interval. Default 'daily' for newly
    composed recipe-bearing reports; legacy/snapshot rows also read 'daily' but are
    inert — the sweep predicate requires recipe_json IS NOT NULL."""
    row = (
        await db.execute(
            text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name='reports' AND column_name='auto_refresh'"
            )
        )
    ).first()
    assert row is not None, "reports.auto_refresh missing — migration 088 not applied"
    data_type, is_nullable, column_default = row
    assert data_type == "text"
    assert is_nullable == "NO"
    assert column_default is not None and "daily" in column_default


async def test_reports_refresh_failure_count_integer_not_null_default_zero(db):
    """Slice C failure ladder: consecutive AUTO-refresh failures (sweep-owned; manual
    refresh and debounce/supersede skips never touch it). Success resets to 0."""
    row = (
        await db.execute(
            text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name='reports' AND column_name='refresh_failure_count'"
            )
        )
    ).first()
    assert row is not None, "reports.refresh_failure_count missing — migration 088 not applied"
    data_type, is_nullable, column_default = row
    assert data_type == "integer"
    assert is_nullable == "NO"
    assert column_default is not None and "0" in column_default


async def test_reports_auto_refresh_paused_at_timestamptz_nullable(db):
    """Slice C pause ladder: set by the sweep after ~7 consecutive failures; cleared
    ONLY by the user's explicit one-click resume (never by a later success)."""
    row = (
        await db.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name='reports' AND column_name='auto_refresh_paused_at'"
            )
        )
    ).first()
    assert row is not None, "reports.auto_refresh_paused_at missing — migration 088 not applied"
    assert row[0] == "timestamp with time zone"
    assert row[1] == "YES"


async def test_report_model_defaults_roundtrip(db):
    """ORM insert without touching the new columns lands the launch defaults —
    auto_refresh='daily', failure count 0, not paused."""
    tenant = await create_test_tenant(db, name="AutoRefreshCorp")
    await set_tenant_context(db, str(tenant.id))
    report = Report(
        tenant_id=tenant.id,
        title="R",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=None,
    )
    db.add(report)
    await db.flush()
    assert report.auto_refresh == "daily"
    assert report.refresh_failure_count == 0
    assert report.auto_refresh_paused_at is None
