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
    # The rendered page's head is the MOCK's title ("Inventory Aging — Week of
    # 8 Sep 2026", spec §A1) plus its sub-line/meta block; "Inventory Aging Weekly"
    # is the Report ROW's series title (§A6), shown by the app's page header, and
    # must not be stamped into the page as its <h1> (readiness-gate finding).
    assert "<h1>Inventory Aging — Week of " in row.rendered_html
    assert "<h1>Inventory Aging Weekly</h1>" not in row.rendered_html
    assert "compared with " in row.rendered_html
    assert "Composed " in row.rendered_html
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


async def test_main_stores_the_real_recipe_so_refresh_and_auto_refresh_selector_work(db, monkeypatch):
    """Refresh-support follow-up: refresh_service.refresh_report / playbooks
    .rebuild_playbook_spec now know how to replay an inventory_aging recipe (see
    this repo's refresh_service/playbooks test files) -- the compose script no
    longer needs the Task 6 501-avoidance workaround of leaving recipe_json None.
    GET /reports/{id}'s has_recipe (recipe_json is not None) legitimately flips
    true, so the report page's Refresh button and auto-refresh interval selector
    are no longer hidden behind a workaround."""
    tenant = await create_test_tenant(db, name="ComposeAgingRecipe")
    await set_tenant_context(db, str(tenant.id))
    payloads, params = _full_fixture()
    _patch_fetch(monkeypatch, payloads)

    report = await compose_inventory_aging.main(tenant.id, params["locations"], db=db)

    assert report.recipe_json is not None
    assert report.recipe_json["playbook"] == {"key": "inventory_aging", "params": {"locations": params["locations"]}}
    assert len(report.recipe_json["sources"]) == 4


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
    assert report.spec_json["title"].startswith("Inventory Aging — Week of ")
    assert report.spec_json["sections"][0]["type"] == "report_head"


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


def test_fetch_payloads_fails_closed_on_a_truncated_bigquery_result(monkeypatch):
    """Gate fix #8: bigquery_service.execute_query's own row-extraction cap sets
    truncated=True on the raw tool result when it drops rows -- _fetch_payloads
    must not silently pass a partial row set through to compute(); it must raise
    SourceTruncated naming the source id, same as rows_from_table_payload does for
    the refresh/headless-compose path."""
    import asyncio

    async def fake_bigquery_sql_execute(params, context):
        return {"columns": ["location", "sku"], "rows": [["Acme", "A-1"]], "truncated": True}

    monkeypatch.setattr("app.mcp.tools.bigquery_tools.bigquery_sql_execute", fake_bigquery_sql_execute)

    class _FakeSession:
        async def execute(self, *a, **k):
            return None

    sources = {"r_items": {"tool": "bigquery_sql", "params": {"query": "SELECT 1"}, "connection_id": None}}
    with pytest.raises(ia.SourceTruncated) as exc:
        asyncio.run(
            compose_inventory_aging._fetch_payloads(
                _FakeSession(),
                sources,
                tenant_id="00000000-0000-0000-0000-000000000000",
                actor_id=None,
                correlation_id="test",
            )
        )
    assert "r_items" in str(exc.value)


async def test_main_propagates_a_truncated_source_without_persisting_a_report(db, monkeypatch):
    """Companion to test_main_propagates_a_fetch_failure_without_persisting_a_report
    above: a truncated source must fail BEFORE compute()/render/persist, exactly
    like any other fetch failure -- nothing rendered, nothing stored."""
    tenant = await create_test_tenant(db, name="ComposeAgingTruncated")
    await set_tenant_context(db, str(tenant.id))

    async def truncated_fetch(db, sources, *, tenant_id, actor_id, correlation_id):
        raise ia.SourceTruncated("r_items")

    monkeypatch.setattr(compose_inventory_aging, "_fetch_payloads", truncated_fetch)

    with pytest.raises(ia.SourceTruncated, match="r_items"):
        await compose_inventory_aging.main(tenant.id, ["Acme", "Globex", "Initech"], db=db)

    await set_tenant_context(db, str(tenant.id))
    rows = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert rows == []


async def test_run_cli_exits_non_zero_naming_the_truncated_source(monkeypatch, capsys):
    """Gate fix #8: the compose script (a scheduled cron invocation) must not crash
    with an unhandled traceback on a truncated source -- it must exit non-zero,
    naming the source, so the caller can tell success from a data-quality failure."""
    import uuid

    async def raising_main(tenant_id, locations, *, db, **kw):
        raise ia.SourceTruncated("r_items")

    monkeypatch.setattr(compose_inventory_aging, "main", raising_main)

    with pytest.raises(SystemExit) as exc:
        await compose_inventory_aging._run_cli(uuid.uuid4(), None)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "r_items" in captured.err
