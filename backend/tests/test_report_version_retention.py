"""Slice C (live-dashboard reports) — version retention.

Cap stored versions per report (spec §6.2: 30) — ``pinned`` exempt, the CURRENT
version never pruned (structurally the parent mirrors it; the guard is explicit
anyway), oldest-unpinned pruned first. Enforced at the single point of version
production (``refresh_report``, post-publish) as a best-effort janitor: a retention
failure must never fail an already-durable publish.
Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md §4C/§6.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.report_version import ReportVersion
from app.services.report.refresh_service import REFRESH_MIN_INTERVAL_SECONDS, refresh_report
from app.services.report.retention import enforce_version_retention
from tests.conftest import create_test_tenant, create_test_user

_SECTIONS = [
    {"type": "heading", "level": 1, "text": "Cash"},
    {"type": "table", "result_id": "r1"},
]


def _recipe():
    return {
        "schema_version": 1,
        "captured_at": "2026-07-06T18:00:00+00:00",
        "sections": _SECTIONS,
        "sources": {"r1": {"tool": "netsuite_suiteql", "params": {"query": "SELECT 1"}, "connection_id": None}},
    }


async def _seed_report_with_versions(db, *, versions: list[tuple[int, bool]], current: int | None = None):
    """versions = [(version, pinned)]; parent.version defaults to max(version)."""
    tenant = await create_test_tenant(db, name="RetainCorp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = Report(
        tenant_id=tenant.id,
        title="R",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=user.id,
        version=current if current is not None else max(v for v, _ in versions),
        recipe_json=_recipe(),
    )
    db.add(report)
    await db.flush()
    for v, pinned in versions:
        db.add(
            ReportVersion(
                tenant_id=tenant.id,
                report_id=report.id,
                version=v,
                spec_json={"sections": []},
                rendered_html=f"<html>v{v}</html>",
                created_by=user.id,
                pinned=pinned,
            )
        )
    await db.flush()
    return tenant, user, report


async def _version_numbers(db, report_id) -> set[int]:
    return set(
        (await db.execute(select(ReportVersion.version).where(ReportVersion.report_id == report_id))).scalars().all()
    )


async def test_prunes_oldest_unpinned_down_to_cap(db):
    tenant, _, report = await _seed_report_with_versions(db, versions=[(v, False) for v in range(1, 6)])
    pruned = await enforce_version_retention(db, report_id=report.id, tenant_id=tenant.id, cap=3)
    assert pruned == 2
    assert await _version_numbers(db, report.id) == {3, 4, 5}  # oldest first


async def test_noop_at_or_under_cap(db):
    tenant, _, report = await _seed_report_with_versions(db, versions=[(1, False), (2, False)])
    assert await enforce_version_retention(db, report_id=report.id, tenant_id=tenant.id, cap=2) == 0
    assert await _version_numbers(db, report.id) == {1, 2}


async def test_pinned_versions_survive_and_may_hold_total_above_cap(db):
    """Pinned exempt (§6.2): pinned rows are never victims, even when that leaves the
    total above the cap — an auditor's pin outranks the janitor."""
    tenant, _, report = await _seed_report_with_versions(
        db, versions=[(1, True), (2, True), (3, True), (4, False), (5, False)]
    )
    pruned = await enforce_version_retention(db, report_id=report.id, tenant_id=tenant.id, cap=2)
    assert pruned == 1  # only v4 deletable: v1-v3 pinned, v5 is current
    assert await _version_numbers(db, report.id) == {1, 2, 3, 5}


async def test_current_version_never_pruned_even_when_unpinned(db):
    tenant, _, report = await _seed_report_with_versions(db, versions=[(1, False), (2, False), (3, False)])
    pruned = await enforce_version_retention(db, report_id=report.id, tenant_id=tenant.id, cap=1)
    assert pruned == 2
    remaining = await _version_numbers(db, report.id)
    assert remaining == {3}
    assert report.version in remaining  # parent's current row survived


async def test_default_cap_comes_from_settings(db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "REPORT_VERSION_RETENTION_CAP", 2)
    tenant, _, report = await _seed_report_with_versions(db, versions=[(v, False) for v in range(1, 5)])
    pruned = await enforce_version_retention(db, report_id=report.id, tenant_id=tenant.id)
    assert pruned == 2
    assert await _version_numbers(db, report.id) == {3, 4}


# --- Integration through refresh_report (the single producer of version rows) ---------


def _fresh_result_str(amount=7):
    return json.dumps(
        {"success": True, "columns": ["account", "amount"], "rows": [["Cash", amount]], "row_count": 1, "query": "q"}
    )


def _patch_executor(monkeypatch):
    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        return _fresh_result_str()

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)


async def _step_past_debounce(db, report):
    report.last_refreshed_at = datetime.now(timezone.utc) - timedelta(seconds=REFRESH_MIN_INTERVAL_SECONDS + 1)
    await db.flush()


async def test_refresh_past_cap_prunes_and_current_stays_max(db, monkeypatch):
    """The handoff pin: parent.version == MAX(report_versions.version) must survive
    pruning — refresh to v4 with cap 2 leaves {3, 4} and the parent at 4."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "REPORT_VERSION_RETENTION_CAP", 2)
    _patch_executor(monkeypatch)
    tenant, user, report = await _seed_report_with_versions(db, versions=[], current=1)

    for _ in range(3):  # v1 lazy-snapshot + v2, then v3, then v4
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
        await _step_past_debounce(db, report)

    row = (await db.execute(select(Report).where(Report.id == report.id))).scalar_one()
    remaining = await _version_numbers(db, report.id)
    assert row.version == 4
    assert remaining == {3, 4}, "cap 2: oldest-unpinned pruned first"
    assert row.version == max(remaining)


async def test_retention_failure_never_fails_the_refresh(db, monkeypatch):
    """The publish is durable before retention runs — a retention error is logged,
    rolled back, and the refresh still returns the new version."""
    _patch_executor(monkeypatch)
    tenant, user, report = await _seed_report_with_versions(db, versions=[], current=1)

    async def broken_retention(*a, **kw):
        raise RuntimeError("janitor exploded")

    monkeypatch.setattr("app.services.report.refresh_service.enforce_version_retention", broken_retention)
    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert updated.version == 2
    assert await _version_numbers(db, report.id) == {1, 2}
    assert updated.rendered_html  # instance readable after the internal rollback