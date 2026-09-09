"""Task 5 (Slice 1) — POST /api/v1/reports/{id}/deliver.

Spec §A5: "report permissions as the existing settings route" — no report.* permission
scope exists anywhere in this codebase (see reports.py's own §6.3 comment), so this
route is gated by EXACTLY what every other report route uses: ``get_current_user`` +
RLS. 401 (no token) and 403 (deactivated tenant) therefore come from that shared
dependency, not from a route-local check — see test_auth_security.py for the same
403-on-deactivated-tenant idiom this file reuses.

Drive/PDF/Excel byte production is stubbed the same way as
``tests/report/test_report_delivery.py`` (module-level monkeypatches of
``report_delivery._build_drive_client`` / ``_render_pdf_bytes`` / ``_render_xlsx_bytes``)
— this file only proves the ROUTE's status codes and response shape; the delivery
service's own behavior (idempotency, audit ordering, failure handling) is covered
there.
"""

from __future__ import annotations

import uuid

from app.core.database import set_tenant_context
from app.core.encryption import encrypt_credentials
from app.models.mcp_connector import McpConnector
from app.models.report import Report
from app.services.report import report_delivery
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers
from tests.report.test_report_delivery import FakeDriveClient


async def _seed_report(db, tenant, user, *, title: str = "Inventory Aging Weekly") -> Report:
    report = Report(
        tenant_id=tenant.id,
        title=title,
        spec_json={"title": title, "sections": []},
        rendered_html="<html><body>REPORT</body></html>",
        created_by=user.id,
    )
    db.add(report)
    await db.flush()
    return report


async def _add_sheets_connector(db, tenant_id) -> McpConnector:
    encrypted = encrypt_credentials({"service_account_json": {"client_email": "sa@test.iam.gserviceaccount.com"}})
    connector = McpConnector(
        tenant_id=tenant_id,
        provider="google_sheets",
        label="test drive",
        server_url="https://sheets.googleapis.com",
        auth_type="service_account",
        encrypted_credentials=encrypted,
        encryption_key_version=1,
        status="active",
        is_enabled=True,
        metadata_json={},
    )
    db.add(connector)
    await db.flush()
    return connector


def _patch_delivery_stack(monkeypatch):
    calls: list[str] = []

    def fake_pdf(report) -> bytes:
        return b"%PDF-FAKE"

    def fake_xlsx(report) -> bytes:
        return b"XLSX-FAKE"

    monkeypatch.setattr(report_delivery, "_render_pdf_bytes", fake_pdf)
    monkeypatch.setattr(report_delivery, "_render_xlsx_bytes", fake_xlsx)

    def factory(credentials, shared_drive_id):
        return FakeDriveClient(calls)

    monkeypatch.setattr(report_delivery, "_build_drive_client", factory)


async def test_deliver_returns_200_with_result(client, db, monkeypatch):
    tenant = await create_test_tenant(db, name="DeliverOK")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _seed_report(db, tenant, user)
    await _add_sheets_connector(db, tenant.id)

    _patch_delivery_stack(monkeypatch)

    resp = await client.post(f"/api/v1/reports/{report.id}/deliver", headers=make_auth_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pdf_file_id"] and body["xlsx_file_id"]
    assert body["pdf_url"] and body["xlsx_url"]
    assert body["folder_id"]
    assert body["delivered_at"]


async def test_deliver_404_for_unknown_report(client, db, monkeypatch):
    tenant = await create_test_tenant(db, name="DeliverUnknown")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))

    _patch_delivery_stack(monkeypatch)

    resp = await client.post(f"/api/v1/reports/{uuid.uuid4()}/deliver", headers=make_auth_headers(user))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Report not found"


async def test_deliver_401_unauthenticated(client, db, monkeypatch):
    tenant = await create_test_tenant(db, name="DeliverUnauth")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _seed_report(db, tenant, user)

    _patch_delivery_stack(monkeypatch)

    resp = await client.post(f"/api/v1/reports/{report.id}/deliver")
    assert resp.status_code == 401


async def test_deliver_403_for_deactivated_tenant(client, db, monkeypatch):
    tenant = await create_test_tenant(db, name="DeliverDeactivated")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _seed_report(db, tenant, user)
    await db.commit()

    tenant.is_active = False

    _patch_delivery_stack(monkeypatch)

    resp = await client.post(f"/api/v1/reports/{report.id}/deliver", headers=make_auth_headers(user))
    assert resp.status_code == 403


async def test_deliver_409_when_no_connector(client, db, monkeypatch):
    tenant = await create_test_tenant(db, name="DeliverNoConnector")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _seed_report(db, tenant, user)

    _patch_delivery_stack(monkeypatch)

    resp = await client.post(f"/api/v1/reports/{report.id}/deliver", headers=make_auth_headers(user))
    assert resp.status_code == 409
    assert resp.json()["detail"]


async def test_get_report_exposes_delivery_json_after_delivery(client, db, monkeypatch):
    tenant = await create_test_tenant(db, name="DeliverExposed")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = await _seed_report(db, tenant, user)
    await _add_sheets_connector(db, tenant.id)

    _patch_delivery_stack(monkeypatch)

    deliver_resp = await client.post(f"/api/v1/reports/{report.id}/deliver", headers=make_auth_headers(user))
    assert deliver_resp.status_code == 200

    get_resp = await client.get(f"/api/v1/reports/{report.id}", headers=make_auth_headers(user))
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["delivery_json"] is not None
    assert body["delivery_json"]["pdf"]["file_id"] == deliver_resp.json()["pdf_file_id"]
