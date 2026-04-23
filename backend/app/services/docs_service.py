"""Google Docs creation via Drive's markdown-to-Doc importer.

Drive's `files.create` auto-converts uploaded `text/markdown` content to a Google
Doc when the target `mimeType` is `application/vnd.google-apps.document`. This
gives us headings, bold/italic, lists, code blocks, and simple tables for free
— no Docs API batchUpdate required.
"""

from __future__ import annotations

import asyncio
import logging
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

_DOC_MIME = "application/vnd.google-apps.document"
_MARKDOWN_MIME = "text/markdown"


def _build_drive_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)


def _normalize_markdown(body: str) -> str:
    """Collapse triple+ blank lines — they produce empty Doc pages in conversion."""
    return re.sub(r"\n{3,}", "\n\n", body)


async def create_doc(
    *,
    credentials: dict | None,
    title: str,
    body_markdown: str,
    parent_id: str | None = None,
) -> dict[str, str]:
    """Create a Google Doc from markdown via Drive multipart upload.

    Returns ``{"doc_id": str, "url": str}``.
    """
    if not credentials:
        raise ValueError("credentials required")
    if not body_markdown or not body_markdown.strip():
        raise ValueError("body_markdown must be non-empty")

    body: dict = {"name": title, "mimeType": _DOC_MIME}
    if parent_id:
        body["parents"] = [parent_id]

    normalized = _normalize_markdown(body_markdown)
    media = MediaInMemoryUpload(normalized.encode("utf-8"), mimetype=_MARKDOWN_MIME)

    def _sync():
        drive = _build_drive_service(credentials)
        result = (
            drive.files()
            .create(
                body=body,
                media_body=media,
                supportsAllDrives=True,
                fields="id,webViewLink",
            )
            .execute()
        )
        return {"doc_id": result["id"], "url": result["webViewLink"]}

    return await asyncio.to_thread(_sync)


async def share_doc(
    *,
    credentials: dict | None,
    doc_id: str,
    email: str,
    role: str = "writer",
) -> dict[str, str]:
    """Grant a user direct access to the Doc. Use when the Doc isn't in a Shared Drive."""
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        drive = _build_drive_service(credentials)
        result = (
            drive.permissions()
            .create(
                fileId=doc_id,
                body={"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail=False,
                supportsAllDrives=True,
            )
            .execute()
        )
        return {"permission_id": result["id"]}

    return await asyncio.to_thread(_sync)
