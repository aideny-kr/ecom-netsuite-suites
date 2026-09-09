"""Drive delivery for a composed report (Slice 1, Task 5).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A5. ``deliver_report_to_drive`` uploads a PDF + Excel workbook of ``report_id`` to
the tenant's Google Drive, under ``Reports / <report title>`` (found-or-created by
name), idempotently keyed by ``period_key``: a same-named existing file is UPDATED
in place, never duplicated. Credentials come from the tenant's ``google_sheets``
service-account ``McpConnector`` (same lookup as ``api/v1/drive_folders.py::
_sheets_connector``) — no connector raises ``DeliveryUnavailable`` (a clean, expected
outcome for a scheduled run: it ends ``blocked``, never a 500).

Two seams exist purely for testability and are patched at the MODULE level by tests
(not passed as function parameters, so the public signature matches the interface
note exactly) rather than passed as arguments:

- ``_build_drive_client(credentials, shared_drive_id) -> DriveClient`` — the real
  implementation (``_GoogleDriveClient``) makes blocking ``googleapiclient`` calls the
  same way ``sheets_service.py``/``docs_service.py`` already do (wrapped in
  ``asyncio.to_thread``); tests substitute an in-memory fake so delivery MECHANICS
  (folder/file idempotency, audit-event ordering, failure handling — this task's own
  scope) are exercised without a live Google API call.
- ``_render_pdf_bytes`` / ``_render_xlsx_bytes`` — the real implementations call
  Task 4's ``render_report_pdf`` (WeasyPrint) and a generic single-sheet workbook via
  Task 3's shared ``build_workbook``. WeasyPrint needs native pango/cairo/gdk-pixbuf
  libraries this repo's dev machines are not guaranteed to have (Task 4's own tests
  skip for the same reason); tests here patch these two functions directly rather than
  skip wholesale, since Task 5 does not own report rendering — it owns getting
  finished bytes into Drive, exactly once per (report, period_key). A report-type-aware
  Excel export (e.g. routing an inventory_aging report through
  ``report_excel.build_inventory_aging_workbook``) is a natural extension once a
  compose+deliver caller has a computed ``AgingReport`` to hand in — no such caller
  exists on this branch yet (Task 1/2's own docstrings note the compute→sections→
  render wiring is a LATER Slice-1 task), so the default here is a generic metadata
  sheet built from the ``Report`` row alone.

Side effects, in order: an ``AuditEvent`` ``report.delivery.started`` (carrying the
idempotency key) is written and COMMITTED before any Drive call — durable proof a
delivery attempt began even if everything after it fails. On success,
``reports.published_drive_url``/``published_at``/``delivery_json`` are written
together in one commit alongside ``report.delivery.completed``. On any failure after
``started``, the transaction is rolled back, `report.delivery.failed` is written in a
fresh mini-transaction (mirroring ``refresh_service.refresh_report``'s failure-audit
pattern), and ``DeliveryFailed`` is raised — ``delivery_json`` is NEVER partially
written on failure.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector
from app.models.report import Report
from app.services import audit_service

logger = logging.getLogger(__name__)

_PDF_MIME = "application/pdf"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_REPORTS_FOLDER_NAME = "Reports"

_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]


class DeliveryUnavailable(Exception):  # noqa: N818 — interface name from spec §A5, not a generic Error
    """No active ``google_sheets`` service-account connector for this tenant.

    A run that raises this ends ``blocked``, never a 500 — the caller (route or
    scheduler) is expected to surface a clear, actionable message."""


class DeliveryFailed(Exception):  # noqa: N818 — interface name from spec §A5, not a generic Error
    """Rendering or a Drive call failed after ``report.delivery.started`` was
    already recorded. ``report.delivery.failed`` has been written; the report's
    published fields / ``delivery_json`` were left untouched."""


@dataclass(frozen=True)
class DeliveryResult:
    pdf_file_id: str
    pdf_url: str
    xlsx_file_id: str
    xlsx_url: str
    folder_id: str
    delivered_at: datetime


class DriveClient(Protocol):
    """Just enough Drive surface for idempotent folder/file delivery. Every method is
    a single logical Drive operation so a fake can record call order without knowing
    anything about Google's actual request/response shapes."""

    async def find_folder(self, *, name: str, parent_id: str | None) -> str | None: ...

    async def create_folder(self, *, name: str, parent_id: str | None) -> str: ...

    async def find_file(self, *, name: str, parent_id: str) -> dict[str, str] | None:
        """Returns ``{"file_id": ..., "url": ...}`` or ``None``."""
        ...

    async def upload_new(self, *, name: str, parent_id: str, content: bytes, mime_type: str) -> dict[str, str]:
        """Returns ``{"file_id": ..., "url": ...}``."""
        ...

    async def update_existing(self, *, file_id: str, content: bytes, mime_type: str) -> dict[str, str]:
        """Returns ``{"file_id": ..., "url": ...}`` (``file_id`` unchanged — this is an
        in-place content replace, never a new file)."""
        ...


def _escape_drive_query(value: str) -> str:
    """Drive's ``q`` string-literal escaping: backslash then single-quote."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class _GoogleDriveClient:
    """Real ``DriveClient`` over ``googleapiclient`` — blocking calls wrapped in
    ``asyncio.to_thread``, matching ``sheets_service.py``/``docs_service.py``'s
    established pattern in this codebase. Not exercised by this task's tests (no live
    Google API in CI); see the module docstring's testability-seam note."""

    def __init__(self, credentials: dict, shared_drive_id: str | None):
        self._credentials = credentials
        self._shared_drive_id = shared_drive_id

    def _service(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(self._credentials, scopes=_DRIVE_SCOPES)
        return build("drive", "v3", credentials=creds)

    async def find_folder(self, *, name: str, parent_id: str | None) -> str | None:
        return await asyncio.to_thread(self._find_folder_sync, name, parent_id)

    def _find_folder_sync(self, name: str, parent_id: str | None) -> str | None:
        q = (
            f"name = '{_escape_drive_query(name)}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        if parent_id:
            q += f" and '{_escape_drive_query(parent_id)}' in parents"
        resp = (
            self._service()
            .files()
            .list(
                q=q,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
                fields="files(id)",
                pageSize=1,
            )
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    async def create_folder(self, *, name: str, parent_id: str | None) -> str:
        return await asyncio.to_thread(self._create_folder_sync, name, parent_id)

    def _create_folder_sync(self, name: str, parent_id: str | None) -> str:
        body: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            body["parents"] = [parent_id]
        result = self._service().files().create(body=body, supportsAllDrives=True, fields="id").execute()
        return result["id"]

    async def find_file(self, *, name: str, parent_id: str) -> dict[str, str] | None:
        return await asyncio.to_thread(self._find_file_sync, name, parent_id)

    def _find_file_sync(self, name: str, parent_id: str) -> dict[str, str] | None:
        q = (
            f"name = '{_escape_drive_query(name)}' and "
            f"'{_escape_drive_query(parent_id)}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        resp = (
            self._service()
            .files()
            .list(
                q=q,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
                fields="files(id,webViewLink)",
                pageSize=1,
            )
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            return None
        return {"file_id": files[0]["id"], "url": files[0].get("webViewLink", "")}

    async def upload_new(self, *, name: str, parent_id: str, content: bytes, mime_type: str) -> dict[str, str]:
        return await asyncio.to_thread(self._upload_new_sync, name, parent_id, content, mime_type)

    def _upload_new_sync(self, name: str, parent_id: str, content: bytes, mime_type: str) -> dict[str, str]:
        from googleapiclient.http import MediaInMemoryUpload

        media = MediaInMemoryUpload(content, mimetype=mime_type)
        result = (
            self._service()
            .files()
            .create(
                body={"name": name, "parents": [parent_id]},
                media_body=media,
                supportsAllDrives=True,
                fields="id,webViewLink",
            )
            .execute()
        )
        return {"file_id": result["id"], "url": result.get("webViewLink", "")}

    async def update_existing(self, *, file_id: str, content: bytes, mime_type: str) -> dict[str, str]:
        return await asyncio.to_thread(self._update_existing_sync, file_id, content, mime_type)

    def _update_existing_sync(self, file_id: str, content: bytes, mime_type: str) -> dict[str, str]:
        from googleapiclient.http import MediaInMemoryUpload

        media = MediaInMemoryUpload(content, mimetype=mime_type)
        result = (
            self._service()
            .files()
            .update(fileId=file_id, media_body=media, supportsAllDrives=True, fields="id,webViewLink")
            .execute()
        )
        return {"file_id": result["id"], "url": result.get("webViewLink", "")}


def _build_drive_client(credentials: dict, shared_drive_id: str | None) -> DriveClient:
    """Patched wholesale by tests (``monkeypatch.setattr(report_delivery,
    "_build_drive_client", fake_factory)``) to hand back an in-memory fake."""
    return _GoogleDriveClient(credentials, shared_drive_id)


def _render_pdf_bytes(report: Report) -> bytes:
    """Patched by tests that don't need real WeasyPrint output — see module
    docstring. Production default: Task 4's ``render_report_pdf`` over the report's
    already-rendered, self-contained HTML (inline SVG, no external fetches — the full
    aged list, if any, is already inlined by the report renderer per Task 2)."""
    from app.services.report.report_pdf import render_report_pdf

    return render_report_pdf(report.rendered_html)


def _render_xlsx_bytes(report: Report) -> bytes:
    """Patched by tests — see module docstring. Production default: a generic
    single-sheet metadata workbook built on Task 3's shared ``build_workbook``, so
    every delivery has SOME Excel artifact even for a report type with no
    report-specific workbook builder wired in yet."""
    from app.services.reconciliation.evidence_service import SheetSpec, build_workbook

    sheet: SheetSpec = {
        "name": "Report",
        "headers": ["Field", "Value"],
        "rows": [
            ["Title", report.title],
            ["Status", report.status],
            ["Version", report.version],
            ["Period", report.period or ""],
            ["Created", report.created_at],
        ],
    }
    return build_workbook([sheet]).getvalue()


async def _sheets_connector(db: AsyncSession, tenant_id: uuid.UUID) -> McpConnector | None:
    """Same lookup as ``api/v1/drive_folders.py::_sheets_connector`` — duplicated
    (not imported) because a service module should not import from the api layer."""
    return (
        (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == tenant_id,
                    McpConnector.provider == "google_sheets",
                    McpConnector.status == "active",
                    McpConnector.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )


async def _find_or_create_folder(client: DriveClient, *, name: str, parent_id: str | None) -> str:
    existing = await client.find_folder(name=name, parent_id=parent_id)
    if existing:
        return existing
    return await client.create_folder(name=name, parent_id=parent_id)


async def _upload_or_update(
    client: DriveClient, *, name: str, parent_id: str, content: bytes, mime_type: str
) -> dict[str, str]:
    existing = await client.find_file(name=name, parent_id=parent_id)
    if existing:
        return await client.update_existing(file_id=existing["file_id"], content=content, mime_type=mime_type)
    return await client.upload_new(name=name, parent_id=parent_id, content=content, mime_type=mime_type)


async def deliver_report_to_drive(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    report_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
    period_key: str,
) -> DeliveryResult:
    """Upload ``report_id``'s PDF + Excel to
    ``<tenant Drive>/Reports/<report title>/``, idempotently keyed by ``period_key``.

    ``actor_id=None`` is valid ONLY with ``actor_type="system"`` (A6's headless
    compose script) — same convention ``refresh_service.refresh_report`` enforces."""
    if actor_id is None and actor_type == "user":
        raise ValueError("deliver_report_to_drive: actor_id=None requires a non-user actor_type")

    await set_tenant_context(db, str(tenant_id))
    report = (
        await db.execute(select(Report).where(Report.id == report_id, Report.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if report is None:
        raise LookupError(f"report {report_id} not found for tenant {tenant_id}")

    connector = await _sheets_connector(db, tenant_id)
    if connector is None:
        raise DeliveryUnavailable("Connect a Google Sheets service account before delivering reports to Drive.")

    envelope = decrypt_credentials(connector.encrypted_credentials)
    credentials = envelope.get("service_account_json", envelope)
    shared_drive_id = (connector.metadata_json or {}).get("shared_drive_id")
    client = _build_drive_client(credentials, shared_drive_id)

    idempotency_key = f"report-delivery:{report_id}:{period_key}"

    # ---- Phase 1: durable "started" record BEFORE any Drive call ----------------
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.delivery.started",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="report",
        resource_id=str(report_id),
        payload={"idempotency_key": idempotency_key, "period_key": period_key},
    )
    await db.commit()

    try:
        # a real Postgres commit clears SET LOCAL GUCs; re-assert tenant scope before
        # any further RLS-protected read/write (the test fixture's savepoint-based
        # commit does not, but re-asserting is a safe no-op there too).
        await set_tenant_context(db, str(tenant_id))

        reports_folder_id = await _find_or_create_folder(client, name=_REPORTS_FOLDER_NAME, parent_id=shared_drive_id)
        series_folder_id = await _find_or_create_folder(client, name=report.title, parent_id=reports_folder_id)

        pdf_bytes = _render_pdf_bytes(report)
        xlsx_bytes = _render_xlsx_bytes(report)

        pdf_name = f"{report.title} — {period_key}.pdf"
        xlsx_name = f"{report.title} — {period_key}.xlsx"

        pdf_result = await _upload_or_update(
            client, name=pdf_name, parent_id=series_folder_id, content=pdf_bytes, mime_type=_PDF_MIME
        )
        xlsx_result = await _upload_or_update(
            client, name=xlsx_name, parent_id=series_folder_id, content=xlsx_bytes, mime_type=_XLSX_MIME
        )

        delivered_at = datetime.now(timezone.utc)
        result = DeliveryResult(
            pdf_file_id=pdf_result["file_id"],
            pdf_url=pdf_result["url"],
            xlsx_file_id=xlsx_result["file_id"],
            xlsx_url=xlsx_result["url"],
            folder_id=series_folder_id,
            delivered_at=delivered_at,
        )

        report.published_drive_url = result.pdf_url
        report.published_at = delivered_at
        report.delivery_json = {
            "pdf": {"file_id": result.pdf_file_id, "url": result.pdf_url},
            "xlsx": {"file_id": result.xlsx_file_id, "url": result.xlsx_url},
            "folder_id": result.folder_id,
            "period_key": period_key,
            "delivered_at": delivered_at.isoformat(),
        }
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="report",
            action="report.delivery.completed",
            actor_id=actor_id,
            actor_type=actor_type,
            resource_type="report",
            resource_id=str(report_id),
            payload={
                "idempotency_key": idempotency_key,
                "pdf_file_id": result.pdf_file_id,
                "xlsx_file_id": result.xlsx_file_id,
            },
        )
        await db.commit()
        return result
    except Exception as exc:
        await db.rollback()
        try:  # durable failure record in a fresh mini-txn (best-effort)
            await set_tenant_context(db, str(tenant_id))
            await audit_service.log_event(
                db=db,
                tenant_id=tenant_id,
                category="report",
                action="report.delivery.failed",
                actor_id=actor_id,
                actor_type=actor_type,
                resource_type="report",
                resource_id=str(report_id),
                payload={"idempotency_key": idempotency_key},
                status="error",
                error_message=str(exc)[:500],
            )
            await db.commit()
        except Exception:
            logger.warning("report.delivery failure-audit write failed", exc_info=True)
        raise DeliveryFailed(str(exc)) from exc
