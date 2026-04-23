from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.drive_rag import extractors


@pytest.mark.asyncio
async def test_extract_google_doc_calls_export_text():
    with patch(
        "app.services.drive_rag.extractors.drive_client.export_google_doc_text",
        new=AsyncMock(return_value="doc body"),
    ):
        text = await extractors.extract_google_doc(credentials={}, file_id="X")
    assert text == "doc body"


@pytest.mark.asyncio
async def test_extract_plain_text_from_download():
    with patch(
        "app.services.drive_rag.extractors.drive_client.download_file_bytes",
        new=AsyncMock(return_value=b"hello world"),
    ):
        text = await extractors.extract_plain_text(credentials={}, file_id="X")
    assert text == "hello world"


@pytest.mark.asyncio
async def test_extract_plain_text_handles_invalid_utf8():
    with patch(
        "app.services.drive_rag.extractors.drive_client.download_file_bytes",
        new=AsyncMock(return_value=b"bad\xffbytes"),
    ):
        text = await extractors.extract_plain_text(credentials={}, file_id="X")
    assert "bad" in text and "bytes" in text


@pytest.mark.asyncio
async def test_extract_pdf_extracts_page_text():
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "pdf page text"
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__.return_value.pages = [mock_page, mock_page]
    with (
        patch(
            "app.services.drive_rag.extractors.drive_client.download_file_bytes",
            new=AsyncMock(return_value=b"%PDF-FAKE"),
        ),
        patch("app.services.drive_rag.extractors.pdfplumber.open", return_value=mock_pdf_ctx),
    ):
        text = await extractors.extract_pdf(credentials={}, file_id="X")
    assert text.count("pdf page text") == 2


@pytest.mark.asyncio
async def test_extract_pdf_tolerates_extract_text_error():
    mock_page_ok = MagicMock()
    mock_page_ok.extract_text.return_value = "good"
    mock_page_bad = MagicMock()
    mock_page_bad.extract_text.side_effect = RuntimeError("boom")
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__.return_value.pages = [mock_page_ok, mock_page_bad]
    with (
        patch(
            "app.services.drive_rag.extractors.drive_client.download_file_bytes",
            new=AsyncMock(return_value=b"%PDF"),
        ),
        patch("app.services.drive_rag.extractors.pdfplumber.open", return_value=mock_pdf_ctx),
    ):
        text = await extractors.extract_pdf(credentials={}, file_id="X")
    assert "good" in text


@pytest.mark.asyncio
async def test_extract_docx_reads_paragraphs():
    mock_doc = MagicMock()
    mock_doc.paragraphs = [MagicMock(text="para 1"), MagicMock(text=""), MagicMock(text="para 2")]
    with (
        patch(
            "app.services.drive_rag.extractors.drive_client.download_file_bytes",
            new=AsyncMock(return_value=b"fake-docx"),
        ),
        patch("app.services.drive_rag.extractors.docx.Document", return_value=mock_doc),
    ):
        text = await extractors.extract_docx(credentials={}, file_id="X")
    assert "para 1" in text and "para 2" in text


@pytest.mark.asyncio
async def test_extract_sheet_joins_rows():
    with patch(
        "app.services.drive_rag.extractors.sheets_service.read_range",
        new=AsyncMock(return_value={"range": "Sheet1", "values": [["a", "b"], ["c", "d"]]}),
    ):
        text = await extractors.extract_sheet(credentials={}, file_id="X")
    assert all(tok in text for tok in ("a", "b", "c", "d"))


@pytest.mark.asyncio
async def test_extract_by_mime_dispatches_google_doc():
    with patch(
        "app.services.drive_rag.extractors.extract_google_doc",
        new=AsyncMock(return_value="doc"),
    ):
        text = await extractors.extract_by_mime(
            credentials={}, file_id="X", mime_type="application/vnd.google-apps.document"
        )
    assert text == "doc"


@pytest.mark.asyncio
async def test_extract_by_mime_raises_for_unsupported():
    with pytest.raises(ValueError):
        await extractors.extract_by_mime(credentials={}, file_id="X", mime_type="image/png")


@pytest.mark.asyncio
async def test_extract_by_mime_respects_timeout():
    """The 30s timeout wraps the extractor; verify it actually timeouts if extraction hangs."""
    import asyncio

    async def _slow(credentials, file_id):
        await asyncio.sleep(60)  # way longer than the timeout
        return "never"

    with (
        patch("app.services.drive_rag.extractors.extract_plain_text", new=_slow),
        patch.object(extractors, "_EXTRACT_TIMEOUT_SECONDS", 0.1),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await extractors.extract_by_mime(credentials={}, file_id="X", mime_type="text/plain")
