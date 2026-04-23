import uuid
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_gather_drive_knowledge_returns_chunks_and_sources_map(monkeypatch):
    from app.services.chat import orchestrator

    mock_chunks = [
        {
            "content": "30-day return window.",
            "source_name": "Returns Policy",
            "web_view_link": "https://docs.google.com/document/d/r/edit",
            "similarity": 0.91,
        },
        {
            "content": "Exchanges allowed.",
            "source_name": "Returns Policy",
            "web_view_link": "https://docs.google.com/document/d/r/edit",
            "similarity": 0.82,
        },
        {
            "content": "Shipping ETA 3-5 days.",
            "source_name": "Shipping FAQ",
            "web_view_link": "https://docs.google.com/document/d/s/edit",
            "similarity": 0.75,
        },
    ]
    monkeypatch.setattr(orchestrator, "retrieve_drive_chunks", AsyncMock(return_value=mock_chunks))

    tenant_id = uuid.uuid4()
    result = await orchestrator._gather_drive_knowledge(db=None, tenant_id=tenant_id, query_text="return policy")
    assert result["chunks"] == mock_chunks
    assert result["sources"] == {
        "Returns Policy": "https://docs.google.com/document/d/r/edit",
        "Shipping FAQ": "https://docs.google.com/document/d/s/edit",
    }


@pytest.mark.asyncio
async def test_gather_drive_knowledge_deduplicates_sources(monkeypatch):
    """Two chunks from the same source file should produce one map entry (first URL wins)."""
    from app.services.chat import orchestrator

    chunks = [
        {
            "content": "a",
            "source_name": "Doc",
            "web_view_link": "https://first",
            "similarity": 0.9,
        },
        {
            "content": "b",
            "source_name": "Doc",
            "web_view_link": "https://second",
            "similarity": 0.8,
        },
    ]
    monkeypatch.setattr(orchestrator, "retrieve_drive_chunks", AsyncMock(return_value=chunks))
    result = await orchestrator._gather_drive_knowledge(db=None, tenant_id=uuid.uuid4(), query_text="q")
    assert result["sources"] == {"Doc": "https://first"}


@pytest.mark.asyncio
async def test_gather_drive_knowledge_handles_empty(monkeypatch):
    from app.services.chat import orchestrator

    monkeypatch.setattr(orchestrator, "retrieve_drive_chunks", AsyncMock(return_value=[]))
    result = await orchestrator._gather_drive_knowledge(db=None, tenant_id=uuid.uuid4(), query_text="q")
    assert result["chunks"] == []
    assert result["sources"] == {}


def test_build_drive_knowledge_block_formats_xml():
    from app.services.chat.orchestrator import _build_drive_knowledge_block

    chunks = [
        {
            "content": "Foo.",
            "source_name": "Returns Policy",
            "web_view_link": "https://x",
            "similarity": 0.9,
        },
        {
            "content": "Bar.",
            "source_name": "Returns Policy",
            "web_view_link": "https://x",
            "similarity": 0.85,
        },
    ]
    block = _build_drive_knowledge_block(chunks)
    assert "<drive_knowledge>" in block
    assert "</drive_knowledge>" in block
    assert "Returns Policy" in block
    assert "Foo." in block and "Bar." in block


def test_build_drive_knowledge_block_empty_returns_empty_string():
    from app.services.chat.orchestrator import _build_drive_knowledge_block

    assert _build_drive_knowledge_block([]) == ""


def test_build_drive_knowledge_block_preserves_raw_content():
    """Content with stray angle brackets must not break the outer XML structure."""
    from app.services.chat.orchestrator import _build_drive_knowledge_block

    chunks = [
        {
            "content": "Use < or >",
            "source_name": "Doc",
            "web_view_link": "https://x",
            "similarity": 0.9,
        },
    ]
    block = _build_drive_knowledge_block(chunks)
    # The block must still be parsable — minimally, the closing tag must
    # appear after the opening tag. Content is kept as-is (LLMs tolerate
    # raw text with stray angle brackets fine).
    assert block.rindex("</drive_knowledge>") > block.index("<drive_knowledge>")
