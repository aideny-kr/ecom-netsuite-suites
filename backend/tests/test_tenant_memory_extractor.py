"""Tests for the LLM tenant-memory concept extractor (pure logic).

Mirrors tests/test_memory_updater.py: mock the adapter via AsyncMock returning
an object with .text_blocks=[json]; assert structured parse; assert [] on garbage.
No DB, no network — runs sandboxed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat import tenant_memory_extractor as ex


class TestExtractConcepts:
    @pytest.mark.asyncio
    async def test_extract_concepts_parses_json(self):
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(
            text_blocks=[
                '{"concepts":[{"name":"Net Revenue","concept_type":"definition",'
                '"plain_english_summary":"Revenue excluding refunds","edges":[],'
                '"confidence":0.9}]}'
            ]
        )
        out = await ex.extract_concepts(
            [{"kind": "learned_rule", "text": "net revenue excludes refunds"}],
            adapter,
            "m",
        )
        assert out[0]["name"] == "Net Revenue"
        assert out[0]["concept_type"] == "definition"
        assert out[0]["plain_english_summary"] == "Revenue excluding refunds"
        assert out[0]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_extract_parses_source_ids(self):
        """Each concept reports the source rows it was distilled from, so the
        backfill can attribute evidence links to the deriving concept."""
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(
            text_blocks=[
                '{"concepts":[{"name":"Net Revenue","concept_type":"definition",'
                '"plain_english_summary":"Revenue excluding refunds","edges":[],'
                '"confidence":0.9,"source_ids":["src-1","src-2"]}]}'
            ]
        )
        out = await ex.extract_concepts(
            [{"kind": "learned_rule", "text": "net revenue excludes refunds", "source_id": "src-1"}],
            adapter,
            "m",
        )
        assert out[0]["source_ids"] == ["src-1", "src-2"]

    @pytest.mark.asyncio
    async def test_extract_source_ids_defaults_to_empty_list(self):
        """A concept without source_ids gets an empty list (never a missing key)."""
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(
            text_blocks=[
                '{"concepts":[{"name":"Net Revenue","concept_type":"definition",'
                '"plain_english_summary":"x","edges":[],"confidence":0.9}]}'
            ]
        )
        out = await ex.extract_concepts([{"x": 1}], adapter, "m")
        assert out[0]["source_ids"] == []

    @pytest.mark.asyncio
    async def test_prompt_requests_source_ids(self):
        """The output-shape instructions must ask the model for source_ids."""
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=["{}"])
        await ex.extract_concepts([{"kind": "x", "text": "y"}], adapter, "m")
        sent_prompt = adapter.create_message.call_args.kwargs["messages"][0]["content"]
        assert "source_ids" in sent_prompt

    @pytest.mark.asyncio
    async def test_extract_returns_empty_on_garbage(self):
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=["no json here"])
        assert await ex.extract_concepts([{"x": 1}], adapter, "m") == []

    @pytest.mark.asyncio
    async def test_extract_returns_empty_on_empty_text_blocks(self):
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=[])
        assert await ex.extract_concepts([{"x": 1}], adapter, "m") == []

    @pytest.mark.asyncio
    async def test_extract_returns_empty_on_invalid_json(self):
        """A JSON-shaped but unparseable blob → swallowed → []."""
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=['{"concepts": [oops not valid]}'])
        assert await ex.extract_concepts([{"x": 1}], adapter, "m") == []

    @pytest.mark.asyncio
    async def test_extract_returns_empty_on_adapter_exception(self):
        adapter = AsyncMock()
        adapter.create_message.side_effect = Exception("API error")
        assert await ex.extract_concepts([{"x": 1}], adapter, "m") == []

    @pytest.mark.asyncio
    async def test_extract_returns_empty_when_no_rows(self):
        """No source rows → no LLM call, empty result."""
        adapter = AsyncMock()
        out = await ex.extract_concepts([], adapter, "m")
        assert out == []
        adapter.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_returns_empty_when_concepts_key_missing(self):
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=['{"something_else": []}'])
        assert await ex.extract_concepts([{"x": 1}], adapter, "m") == []

    @pytest.mark.asyncio
    async def test_extract_passes_rows_into_prompt_via_string_replace(self):
        """ROWS placeholder is string-replaced, not f-string interpolated.

        A raw brace in the row text must not blow up prompt assembly.
        """
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=["{}"])
        await ex.extract_concepts(
            [{"kind": "learned_rule", "text": "use {custbody_x} here"}],
            adapter,
            "m",
        )
        sent_prompt = adapter.create_message.call_args.kwargs["messages"][0]["content"]
        assert "{{ROWS}}" not in sent_prompt
        assert "custbody_x" in sent_prompt


class TestPromptHygiene:
    @pytest.mark.asyncio
    async def test_prompt_has_no_hardcoded_tenant_columns(self):
        """no-prompt-pollution: the static prompt must carry only behavioral
        guidance — no hardcoded tenant schema (column/table script IDs)."""
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=["{}"])
        await ex.extract_concepts([{"kind": "x", "text": "y"}], adapter, "m")
        sent_prompt = adapter.create_message.call_args.kwargs["messages"][0]["content"]
        prefix = sent_prompt.split("y")[0]  # strip the injected row text
        for needle in ("custbody", "custcol", "custitem", "customrecord", "customlist"):
            assert needle not in prefix.lower()

    @pytest.mark.asyncio
    async def test_prompt_instructs_no_numbers(self):
        """no-LLM-numbers: the prompt must instruct the model to never
        restate/compute/total/invent numbers."""
        adapter = AsyncMock()
        adapter.create_message.return_value = SimpleNamespace(text_blocks=["{}"])
        await ex.extract_concepts([{"kind": "x", "text": "y"}], adapter, "m")
        sent_prompt = adapter.create_message.call_args.kwargs["messages"][0]["content"]
        assert "number" in sent_prompt.lower()
