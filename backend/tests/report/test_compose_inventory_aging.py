"""Tests for backend/scripts/compose_inventory_aging.py (Slice 1, Task 6).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A6. ``main(tenant_id, locations, *, db)`` composes the ``inventory_aging`` playbook
headlessly (actor_type "system") and persists it as a Report row titled "Inventory
Aging Weekly", ``auto_refresh="off"`` (Slice 2's schedule owns the cadence, not this
report's own auto-refresh sweep), pinned to the dashboard.

``compose_playbook_report`` (Task 1) fails CLOSED with a 501 for any playbook whose
recipe's first section isn't ``financial_statement`` -- inventory_aging's own
``_INVENTORY_AGING_SECTION_TYPES`` never is one, so this script does NOT go through
that shared compose path. It builds the sources, computes the ``AgingReport`` (Task 1),
renders it with Task 2's ``build_inventory_aging_sections``/``render_report_html``, and
writes the ``Report`` row directly -- the same steps ``compose_playbook_report`` takes
for a financial_statement, minus the statement-only branches that don't apply here.

The BigQuery dispatch is a module-level seam, ``compose_inventory_aging._fetch_payloads``
-- patched here with synthetic payloads (reusing Task 1's own fixture builder, never
live BigQuery), the same "patch the byte/IO producer at module level" pattern Task 5's
``report_delivery._build_drive_client``/``_render_pdf_bytes`` already established.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import scripts.compose_inventory_aging as compose_inventory_aging
from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.report import Report
from app.services.report import inventory_aging as ia
from tests.conftest import create_test_tenant
from tests.report.test_inventory_aging import _full_fixture


def _patch_fetch(monkeypatch, payloads: dict[str, list[dict]]):
    calls: list[str] = []

    async def fake_fetch(db, sources, *, tenant_id, actor_id, correlation_id):
        calls.append(correlation_id)
        assert set(sources) == set(ia.RESULT_IDS)
        return payloads

    monkeypatch.setattr(compose_inventory_aging, "_fetch_payloads", fake_fetch)
    return calls


async def test_main_composes_a_report_titled_inventory_aging_weekly(db, monkeypatch):
    tenant = await create_test_tenant(db, name="ComposeAging")
    await set_tenant_context(db, str(tenant.id))
    payloads, params = _full_fixture()
    _patch_fetch(monkeypatch, payloads)

    report = await compose_inventory_aging.main(tenant.id, params["locations"], db=db)

    assert report.title == "Inventory Aging Weekly"
    assert report.auto_refresh == "off"
    assert report.dashboard_pinned_at is not None


async def test_main_persists_the_report_row_in_the_db(db, monkeypatch):
    tenant = await create_test_tenant(db, name="ComposeAgingPersist")
    await set_tenant_context(db, str(tenant.id))
    payloads, params = _full_fixture()
    _patch_fetch(monkeypatch, payloads)

    report = await compose_inventory_aging.main(tenant.id, params["locations"], db=db)

    await set_tenant_context(db, str(tenant.id))
    row = (await db.execute(select(Report).where(Report.id == report.id))).scalar_one()
    assert row.tenant_id == tenant.id
    assert row.status == "draft"
    assert "Inventory Aging Weekly" in row.rendered_html
    # the mock's own section heading, so a real render actually happened -- not just
    # a title stamped on an empty page.
    assert "Watch items" in row.rendered_html
    assert "<script" not in row.rendered_html.lower()


async def test_main_writes_a_report_compose_audit_event_as_system_actor(db, monkeypatch):
    tenant = await create_test_tenant(db, name="ComposeAgingAudit")
    await set_tenant_context(db, str(tenant.id))
    payloads, params = _full_fixture()
    _patch_fetch(monkeypatch, payloads)

    report = await compose_inventory_aging.main(tenant.id, params["locations"], db=db)

    await set_tenant_context(db, str(tenant.id))
    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.action == "report.compose",
                AuditEvent.resource_id == str(report.id),
            )
        )
    ).scalar_one()
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert event.payload.get("playbook") == "inventory_aging"


async def test_main_leaves_recipe_json_none_so_refresh_stays_hidden(db, monkeypatch):
    """Review finding (fix round 1): ``refresh_service.refresh_report`` /
    ``report_service.assemble_spec`` only understand ``financial_statement``-shaped
    recipes -- the exact reason ``compose_playbook_report`` (Task 1) fails CLOSED with
    a 501 for this playbook instead of ever reaching that shared path. A non-None
    ``recipe_json`` here would flip GET /reports/{id}'s ``has_recipe`` to true, which
    is the ONLY gate the report page uses to show the Refresh button and the
    auto-refresh interval selector (``frontend/.../reports/[id]/page.tsx``) -- so
    clicking Refresh would commit a real debounce stamp, dispatch a live BigQuery
    source, and only THEN crash inside ``normalize_and_validate_sections`` with an
    unhandled 500 (none of watch_items/kpi_cards/mid_row/bucket_table/top_positions
    is a recognized section type). Until refresh support for this playbook shape
    actually exists, this report must be snapshot-only: no recipe, no Refresh."""
    tenant = await create_test_tenant(db, name="ComposeAgingRecipe")
    await set_tenant_context(db, str(tenant.id))
    payloads, params = _full_fixture()
    _patch_fetch(monkeypatch, payloads)

    report = await compose_inventory_aging.main(tenant.id, params["locations"], db=db)

    assert report.recipe_json is None


async def test_main_spec_json_is_actually_json_safe(db, monkeypatch):
    """Decimal/date-bearing dataclasses (``AgingReport``'s sections) must be converted
    before persisting -- a bare Decimal/date would fail JSONB serialization outright,
    so a successful DB round trip here IS the assertion (T2 gate pattern: prove it by
    actually inserting, not by inspecting the object graph)."""
    tenant = await create_test_tenant(db, name="ComposeAgingJsonSafe")
    await set_tenant_context(db, str(tenant.id))
    payloads, params = _full_fixture()
    _patch_fetch(monkeypatch, payloads)

    report = await compose_inventory_aging.main(tenant.id, params["locations"], db=db)

    assert isinstance(report.spec_json, dict)
    assert report.spec_json["title"] == "Inventory Aging Weekly"


async def test_main_uses_default_locations_when_none_given(db, monkeypatch):
    tenant = await create_test_tenant(db, name="ComposeAgingDefaults")
    await set_tenant_context(db, str(tenant.id))
    seen_sources: dict = {}

    async def fake_fetch(db, sources, *, tenant_id, actor_id, correlation_id):
        seen_sources.update(sources)
        payloads, _ = _full_fixture()
        return payloads

    monkeypatch.setattr(compose_inventory_aging, "_fetch_payloads", fake_fetch)

    await compose_inventory_aging.main(tenant.id, None, db=db)

    query = seen_sources["r_items"]["params"]["query"]
    for loc in ia.DEFAULT_LOCATIONS:
        assert f"'{loc}'" in query


async def test_main_propagates_a_fetch_failure_without_persisting_a_report(db, monkeypatch):
    tenant = await create_test_tenant(db, name="ComposeAgingFail")
    await set_tenant_context(db, str(tenant.id))

    async def failing_fetch(db, sources, *, tenant_id, actor_id, correlation_id):
        raise RuntimeError("source r_items (bigquery_sql) failed: simulated")

    monkeypatch.setattr(compose_inventory_aging, "_fetch_payloads", failing_fetch)

    with pytest.raises(RuntimeError, match="simulated"):
        await compose_inventory_aging.main(tenant.id, ["Acme", "Globex", "Initech"], db=db)

    await set_tenant_context(db, str(tenant.id))
    count = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert count == []


def test_fetch_payloads_converts_columns_rows_into_list_of_dicts(monkeypatch):
    """Production seam sanity: bigquery_sql_execute returns {"columns", "rows"} with
    rows as POSITIONAL lists (see bigquery_service.execute_query) -- _fetch_payloads
    must zip them into the list[dict] shape inventory_aging.compute() reads
    (``row["location"]`` etc.), never pass the raw positional rows through."""
    import asyncio

    async def fake_bigquery_sql_execute(params, context):
        return {"columns": ["location", "sku"], "rows": [["Acme", "A-1"], ["Globex", "G-1"]]}

    monkeypatch.setattr("app.mcp.tools.bigquery_tools.bigquery_sql_execute", fake_bigquery_sql_execute)

    class _FakeSession:
        async def execute(self, *a, **k):
            return None

    sources = {"r_items": {"tool": "bigquery_sql", "params": {"query": "SELECT 1"}, "connection_id": None}}
    result = asyncio.run(
        compose_inventory_aging._fetch_payloads(
            _FakeSession(),
            sources,
            tenant_id="00000000-0000-0000-0000-000000000000",
            actor_id=None,
            correlation_id="test",
        )
    )
    assert result == {"r_items": [{"location": "Acme", "sku": "A-1"}, {"location": "Globex", "sku": "G-1"}]}
