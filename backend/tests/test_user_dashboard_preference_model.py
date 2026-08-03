# backend/tests/test_user_dashboard_preference_model.py
"""Migration 092 + UserDashboardPreference model — catalog shape, RLS, unique
constraint, and tombstone-on-report-delete behaviour (report_id SET NULL, not
cascade-deleted).

Pattern mirrors test_report_migration.py (catalog checks) and
test_agent_lab_model.py (IntegrityError round-trip)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.user_dashboard_preference import UserDashboardPreference
from tests.conftest import create_test_tenant, create_test_user


async def _make_report(db: AsyncSession, tenant_id) -> Report:
    report = Report(
        tenant_id=tenant_id,
        title="R",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=None,
    )
    db.add(report)
    await db.flush()
    return report


async def test_table_columns_exist(db: AsyncSession):
    cols = (
        (
            await db.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='user_dashboard_preferences'")
            )
        )
        .scalars()
        .all()
    )
    assert {"id", "tenant_id", "user_id", "report_id", "updated_at"} <= set(cols)


async def test_unique_constraint_exists(db: AsyncSession):
    uq = (
        await db.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname='uq_user_dashboard_preference_tenant_user' AND contype='u'"
            )
        )
    ).scalar()
    assert uq == 1, "(tenant_id, user_id) unique constraint missing"


async def test_rls_is_forced_with_tenant_policy(db: AsyncSession):
    rls = (
        await db.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='user_dashboard_preferences'")
        )
    ).first()
    assert rls is not None and rls[0] and rls[1], "user_dashboard_preferences must have RLS ENABLED + FORCE'd"

    pol = (
        await db.execute(
            text(
                "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
                "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                "WHERE c.relname='user_dashboard_preferences'"
            )
        )
    ).first()
    assert pol is not None, "user_dashboard_preferences has no RLS policy"
    using, with_check = pol
    assert using and "get_current_tenant_id()" in using
    assert with_check and "get_current_tenant_id()" in with_check
    assert "00000000-0000-0000-0000-000000000000" not in (using + with_check)


async def test_model_roundtrip(db: AsyncSession):
    tenant = await create_test_tenant(db, name="WallCorp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _make_report(db, tenant.id)

    pref = UserDashboardPreference(tenant_id=tenant.id, user_id=user.id, report_id=report.id)
    db.add(pref)
    await db.flush()

    assert pref.id is not None
    assert pref.updated_at is not None


async def test_duplicate_tenant_user_violates_unique_constraint(db: AsyncSession):
    tenant = await create_test_tenant(db, name="WallCorp2")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report_1 = await _make_report(db, tenant.id)
    report_2 = await _make_report(db, tenant.id)

    db.add(UserDashboardPreference(tenant_id=tenant.id, user_id=user.id, report_id=report_1.id))
    await db.flush()

    db.add(UserDashboardPreference(tenant_id=tenant.id, user_id=user.id, report_id=report_2.id))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_deleting_report_tombstones_preference(db: AsyncSession):
    """report_id is nullable with ON DELETE SET NULL (not CASCADE, migration
    092): deleting the selected report must leave the preference row in place
    with report_id NULLed out, not delete it — a tombstone, so GET /dashboard
    can tell "chose it, then it was deleted" apart from "never chosen"."""
    tenant = await create_test_tenant(db, name="WallCorp3")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _make_report(db, tenant.id)

    pref = UserDashboardPreference(tenant_id=tenant.id, user_id=user.id, report_id=report.id)
    db.add(pref)
    await db.flush()
    pref_id = pref.id

    await db.delete(report)
    await db.flush()

    row = (
        await db.execute(
            text("SELECT report_id FROM user_dashboard_preferences WHERE id = :id"),
            {"id": str(pref_id)},
        )
    ).first()
    assert row is not None, "deleting the referenced report must NOT delete the preference row (tombstone, not cascade)"
    assert row[0] is None, "report_id must be set NULL when the referenced report is deleted"
