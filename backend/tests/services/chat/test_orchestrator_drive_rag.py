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


# ---------------------------------------------------------------------------
# User-inserted Drive mentions — `[Name](drive_url)` in chat input
# ---------------------------------------------------------------------------


def test_extract_drive_mentions_single():
    from app.services.chat.orchestrator import _extract_drive_mentions

    msg = "Please summarize [Returns Policy](https://docs.google.com/document/d/abc/edit)."
    assert _extract_drive_mentions(msg) == {"Returns Policy": "https://docs.google.com/document/d/abc/edit"}


def test_extract_drive_mentions_multiple():
    from app.services.chat.orchestrator import _extract_drive_mentions

    msg = (
        "Compare [Q1 Analysis](https://docs.google.com/document/d/q1/edit) "
        "with [Q2 Analysis](https://drive.google.com/file/d/q2/view)."
    )
    result = _extract_drive_mentions(msg)
    assert result == {
        "Q1 Analysis": "https://docs.google.com/document/d/q1/edit",
        "Q2 Analysis": "https://drive.google.com/file/d/q2/view",
    }


def test_extract_drive_mentions_ignores_non_drive_urls():
    """Only Drive / Docs URLs count — GitHub, bare domains, etc. are prose."""
    from app.services.chat.orchestrator import _extract_drive_mentions

    msg = (
        "See [our repo](https://github.com/foo/bar) and [docs](https://example.com) "
        "but cite [Returns Policy](https://docs.google.com/document/d/abc/edit)."
    )
    assert _extract_drive_mentions(msg) == {"Returns Policy": "https://docs.google.com/document/d/abc/edit"}


def test_extract_drive_mentions_handles_trailing_punctuation():
    """Period / comma / paren adjacent to the URL should not be captured."""
    from app.services.chat.orchestrator import _extract_drive_mentions

    msg = "Did you see [Returns Policy](https://docs.google.com/document/d/abc/edit)?"
    result = _extract_drive_mentions(msg)
    assert result["Returns Policy"] == "https://docs.google.com/document/d/abc/edit"


def test_extract_drive_mentions_inside_sentence():
    """Mention in the middle of prose — regex must not be anchored."""
    from app.services.chat.orchestrator import _extract_drive_mentions

    msg = (
        "Before you answer, look at [Returns Policy]"
        "(https://docs.google.com/document/d/abc/edit) and tell me the window length."
    )
    assert "Returns Policy" in _extract_drive_mentions(msg)


def test_extract_drive_mentions_empty_returns_empty_dict():
    from app.services.chat.orchestrator import _extract_drive_mentions

    assert _extract_drive_mentions("") == {}
    assert _extract_drive_mentions("no mentions here") == {}


def test_extract_drive_mentions_ignores_broken_markdown():
    """Missing paren / bracket / url — no match, no raise."""
    from app.services.chat.orchestrator import _extract_drive_mentions

    assert _extract_drive_mentions("[unclosed (https://docs.google.com/document/d/a)") == {}
    assert _extract_drive_mentions("[name] (https://docs.google.com/document/d/a)") == {}


def test_merge_drive_mentions_creates_drive_sources():
    """When context has no drive_sources key, create it from mentions alone."""
    from app.services.chat.orchestrator import _merge_drive_mentions

    context: dict = {}
    msg = "Summarize [Returns Policy](https://docs.google.com/document/d/r/edit)."
    merged = _merge_drive_mentions(context, msg)
    assert merged == [("Returns Policy", "https://docs.google.com/document/d/r/edit")]
    assert context["drive_sources"] == {"Returns Policy": "https://docs.google.com/document/d/r/edit"}


def test_merge_drive_mentions_respects_existing_retrieval_sources():
    """An existing drive_sources entry from RAG retrieval wins over a user mention
    for the same source name — retrieval URL is authoritative."""
    from app.services.chat.orchestrator import _merge_drive_mentions

    context = {"drive_sources": {"Returns Policy": "https://authoritative/retrieval"}}
    msg = "Check [Returns Policy](https://docs.google.com/document/d/other/edit)."
    merged = _merge_drive_mentions(context, msg)
    assert merged == []  # Nothing new was added
    assert context["drive_sources"] == {"Returns Policy": "https://authoritative/retrieval"}


def test_merge_drive_mentions_adds_novel_names_while_keeping_existing():
    from app.services.chat.orchestrator import _merge_drive_mentions

    context = {"drive_sources": {"Returns Policy": "https://rp/from-retrieval"}}
    msg = (
        "Compare [Returns Policy](https://docs.google.com/document/d/rp/edit) "
        "with [Shipping FAQ](https://docs.google.com/document/d/sf/edit)."
    )
    merged = _merge_drive_mentions(context, msg)
    assert merged == [("Shipping FAQ", "https://docs.google.com/document/d/sf/edit")]
    assert context["drive_sources"] == {
        "Returns Policy": "https://rp/from-retrieval",
        "Shipping FAQ": "https://docs.google.com/document/d/sf/edit",
    }


def test_merge_drive_mentions_no_mentions_is_noop():
    from app.services.chat.orchestrator import _merge_drive_mentions

    context = {"drive_sources": {"A": "https://a"}}
    before = dict(context["drive_sources"])
    assert _merge_drive_mentions(context, "no mentions here") == []
    assert context["drive_sources"] == before


def test_build_drive_mentions_hint_formats_markdown_list():
    from app.services.chat.orchestrator import _build_drive_mentions_hint

    hint = _build_drive_mentions_hint(
        [
            ("Returns Policy", "https://rp"),
            ("Shipping FAQ", "https://sf"),
        ]
    )
    assert "User-mentioned Drive files this turn" in hint
    assert "- [Returns Policy](https://rp)" in hint
    assert "- [Shipping FAQ](https://sf)" in hint
    # Should be prefixed with blank lines so it doesn't collide with prior prompt content
    assert hint.startswith("\n\n")


def test_build_drive_mentions_hint_empty_returns_empty_string():
    from app.services.chat.orchestrator import _build_drive_mentions_hint

    assert _build_drive_mentions_hint([]) == ""
