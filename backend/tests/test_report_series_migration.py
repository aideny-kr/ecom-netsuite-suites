# backend/tests/test_report_series_migration.py
"""Migration 093 + ReportSeries model — catalog shape, RLS, unique constraints
(both the plain (tenant_id, playbook_key) one and the partial (series_id, period)
one on reports), and the ON DELETE SET NULL behaviour that keeps a series'
reports alive after the series itself is deleted.

Pattern mirrors test_report_migration.py (catalog checks) and
test_user_dashboard_preference_model.py (IntegrityError round-trips)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.report_series import ReportSeries
from tests.conftest import create_test_tenant


async def _make_series(db: AsyncSession, tenant_id, playbook_key: str = "cash-flow") -> ReportSeries:
    series = ReportSeries(tenant_id=tenant_id, playbook_key=playbook_key, created_by=None)
    db.add(series)
    await db.flush()
    return series


async def _make_report(db: AsyncSession, tenant_id, *, series_id=None, period=None) -> Report:
    report = Report(
        tenant_id=tenant_id,
        title="R",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=None,
        series_id=series_id,
        period=period,
    )
    db.add(report)
    await db.flush()
    return report


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


async def test_report_series_table_columns_exist(db: AsyncSession):
    cols = (
        (await db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='report_series'")))
        .scalars()
        .all()
    )
    assert {"id", "tenant_id", "playbook_key", "created_by", "created_at"} <= set(cols)


async def test_reports_series_id_and_period_columns_exist_and_nullable(db: AsyncSession):
    rows = dict(
        (
            await db.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name='reports' AND column_name IN ('series_id', 'period')"
                )
            )
        ).all()
    )
    assert rows == {"series_id": "YES", "period": "YES"}, "reports.series_id/period missing — migration 093 not applied"


async def test_report_series_unique_tenant_playbook_constraint_exists(db: AsyncSession):
    uq = (
        await db.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname='uq_report_series_tenant_playbook' AND contype='u'")
        )
    ).scalar()
    assert uq == 1, "(tenant_id, playbook_key) unique constraint missing"


async def test_reports_series_period_partial_unique_index_exists(db: AsyncSession):
    row = (
        await db.execute(
            text(
                "SELECT indisunique, pg_get_expr(indpred, indrelid) FROM pg_index "
                "WHERE indexrelid = 'uq_reports_series_id_period'::regclass"
            )
        )
    ).first()
    assert row is not None, "partial unique index on reports(series_id, period) missing"
    is_unique, predicate = row
    assert is_unique is True
    assert predicate is not None and "series_id" in predicate and "period" in predicate


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------


async def test_report_series_rls_is_forced_with_tenant_policy(db: AsyncSession):
    rls = (
        await db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='report_series'"))
    ).first()
    assert rls is not None and rls[0] and rls[1], "report_series must have RLS ENABLED + FORCE'd"

    pol = (
        await db.execute(
            text(
                "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
                "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname='report_series'"
            )
        )
    ).first()
    assert pol is not None, "report_series has no RLS policy"
    using, with_check = pol
    assert using and "get_current_tenant_id()" in using
    assert with_check and "get_current_tenant_id()" in with_check
    # report_series rows are never SYSTEM-owned — the OR-SYSTEM read clause must NOT appear.
    assert "00000000-0000-0000-0000-000000000000" not in (using + with_check)


# ---------------------------------------------------------------------------
# Constraint round-trips: they must BITE, not merely exist
# ---------------------------------------------------------------------------


async def test_report_series_model_roundtrip(db: AsyncSession):
    tenant = await create_test_tenant(db, name="SeriesCorp")
    await set_tenant_context(db, str(tenant.id))
    series = await _make_series(db, tenant.id)
    assert series.id is not None
    assert series.created_at is not None


async def test_duplicate_tenant_playbook_violates_unique_constraint(db: AsyncSession):
    tenant = await create_test_tenant(db, name="SeriesCorp2")
    await set_tenant_context(db, str(tenant.id))
    await _make_series(db, tenant.id, playbook_key="cash-flow")

    db.add(ReportSeries(tenant_id=tenant.id, playbook_key="cash-flow", created_by=None))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_same_playbook_key_different_tenant_is_allowed(db: AsyncSession):
    """The unique constraint is scoped to (tenant_id, playbook_key) — two different
    tenants tracking the same playbook must not collide."""
    tenant_a = await create_test_tenant(db, name="SeriesCorp3A")
    tenant_b = await create_test_tenant(db, name="SeriesCorp3B")

    await set_tenant_context(db, str(tenant_a.id))
    await _make_series(db, tenant_a.id, playbook_key="cash-flow")

    await set_tenant_context(db, str(tenant_b.id))
    series_b = await _make_series(db, tenant_b.id, playbook_key="cash-flow")
    assert series_b.id is not None


async def test_duplicate_series_period_violates_partial_unique_index(db: AsyncSession):
    tenant = await create_test_tenant(db, name="SeriesCorp4")
    await set_tenant_context(db, str(tenant.id))
    series = await _make_series(db, tenant.id)
    await _make_report(db, tenant.id, series_id=series.id, period="Jun 2026")

    db.add(
        Report(
            tenant_id=tenant.id,
            title="R2",
            spec_json={"sections": []},
            rendered_html="<html></html>",
            created_by=None,
            series_id=series.id,
            period="Jun 2026",
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_same_series_different_period_is_allowed(db: AsyncSession):
    tenant = await create_test_tenant(db, name="SeriesCorp5")
    await set_tenant_context(db, str(tenant.id))
    series = await _make_series(db, tenant.id)
    await _make_report(db, tenant.id, series_id=series.id, period="Jun 2026")
    report_jul = await _make_report(db, tenant.id, series_id=series.id, period="Jul 2026")
    assert report_jul.id is not None


async def test_two_null_series_period_reports_are_allowed(db: AsyncSession):
    """The partial index only applies WHERE both columns are non-null — snapshot
    reports (the overwhelming majority; no backfill) must be free to coexist with
    series_id=NULL, period=NULL."""
    tenant = await create_test_tenant(db, name="SeriesCorp6")
    await set_tenant_context(db, str(tenant.id))
    r1 = await _make_report(db, tenant.id)
    r2 = await _make_report(db, tenant.id)
    assert r1.id is not None and r2.id is not None
    assert r1.series_id is None and r1.period is None
    assert r2.series_id is None and r2.period is None


async def test_deleting_series_leaves_reports_with_series_id_null(db: AsyncSession):
    """reports.series_id FK is ON DELETE SET NULL (not CASCADE): deleting the series
    must leave its reports in place with series_id NULLed out — they remain valid
    standalone artifacts, just no longer tracked. Verified via raw SELECT (not the ORM
    session, and not a relationship()-driven cascade — there is no relationship()
    between ReportSeries and Report, so this can only be the database's FK action)."""
    tenant = await create_test_tenant(db, name="SeriesCorp7")
    await set_tenant_context(db, str(tenant.id))
    series = await _make_series(db, tenant.id)
    report = await _make_report(db, tenant.id, series_id=series.id, period="Jun 2026")
    report_id = report.id

    await db.delete(series)
    await db.flush()

    row = (await db.execute(text("SELECT id, series_id FROM reports WHERE id = :id"), {"id": str(report_id)})).first()
    assert row is not None, "deleting the series must NOT delete its reports"
    assert row[1] is None, "series_id must be set NULL when the referenced series is deleted"
