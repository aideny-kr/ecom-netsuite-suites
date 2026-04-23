"""Per-mime-type text extractors for Drive files."""

from __future__ import annotations

import asyncio
import io
import logging

import docx
import pdfplumber

from app.services import sheets_service
from app.services.drive_rag import drive_client

logger = logging.getLogger(__name__)

_EXTRACT_TIMEOUT_SECONDS = 30.0


async def extract_google_doc(*, credentials: dict, file_id: str) -> str:
    return await drive_client.export_google_doc_text(credentials=credentials, file_id=file_id)


async def extract_plain_text(*, credentials: dict, file_id: str) -> str:
    raw = await drive_client.download_file_bytes(credentials=credentials, file_id=file_id)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


async def extract_pdf(*, credentials: dict, file_id: str) -> str:
    raw = await drive_client.download_file_bytes(credentials=credentials, file_id=file_id)

    def _sync() -> str:
        pieces: list[str] = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t:
                    pieces.append(t)
        return "\n\n".join(pieces)

    return await asyncio.to_thread(_sync)


async def extract_docx(*, credentials: dict, file_id: str) -> str:
    raw = await drive_client.download_file_bytes(credentials=credentials, file_id=file_id)

    def _sync() -> str:
        doc = docx.Document(io.BytesIO(raw))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text and p.text.strip())

    return await asyncio.to_thread(_sync)


async def extract_sheet(*, credentials: dict, file_id: str) -> str:
    result = await sheets_service.read_range(credentials=credentials, spreadsheet_id=file_id, range_str="Sheet1")
    rows = result.get("values", [])
    return "\n".join("\t".join(str(c) for c in row) for row in rows)


# Map mime types to extractor function *names* (not references) so that
# monkeypatching the module-level attribute in tests is honored at dispatch time.
_DISPATCH: dict[str, str] = {
    "application/vnd.google-apps.document": "extract_google_doc",
    "application/pdf": "extract_pdf",
    "application/vnd.google-apps.spreadsheet": "extract_sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "extract_docx",
    "text/plain": "extract_plain_text",
    "text/markdown": "extract_plain_text",
}


async def extract_by_mime(*, credentials: dict, file_id: str, mime_type: str) -> str:
    fn_name = _DISPATCH.get(mime_type)
    if fn_name is None:
        raise ValueError(f"unsupported mime type: {mime_type}")
    fn = globals()[fn_name]
    return await asyncio.wait_for(
        fn(credentials=credentials, file_id=file_id),
        timeout=_EXTRACT_TIMEOUT_SECONDS,
    )
