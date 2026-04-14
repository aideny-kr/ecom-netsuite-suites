"""Tests for tenant_resolver — NER extraction prompt and graceful-degradation contract."""

import asyncio
import uuid

import pytest

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse
from app.services.chat.tenant_resolver import EXTRACTOR_SYSTEM_PROMPT, TenantEntityResolver


class TestExtractorPrompt:
    """Verify the NER prompt covers the entity types we need."""

    def test_prompt_extracts_status_values(self):
        assert "Failed" in EXTRACTOR_SYSTEM_PROMPT
        assert "Completed" in EXTRACTOR_SYSTEM_PROMPT
        assert "Pending" in EXTRACTOR_SYSTEM_PROMPT

    def test_prompt_extracts_custom_records(self):
        assert "Inventory Processor" in EXTRACTOR_SYSTEM_PROMPT

    def test_prompt_extracts_saved_searches(self):
        assert "Saved search" in EXTRACTOR_SYSTEM_PROMPT or "report" in EXTRACTOR_SYSTEM_PROMPT.lower()

    def test_prompt_excludes_generic_terms(self):
        assert "sales order" in EXTRACTOR_SYSTEM_PROMPT.lower()  # mentioned as DO NOT extract
        assert "Do NOT extract" in EXTRACTOR_SYSTEM_PROMPT


class _StallingAdapter(BaseLLMAdapter):
    """Test double: simulates an Anthropic socket that never returns bytes."""

    async def create_message(self, **_kwargs) -> LLMResponse:
        # Sleep far longer than any reasonable caller-side timeout.
        await asyncio.sleep(30)
        return LLMResponse()

    def build_tool_result_message(self, tool_results):
        return {"role": "user", "content": tool_results}

    def build_assistant_message(self, response):
        return {"role": "assistant", "content": []}


class TestResolverTimeoutContract:
    """A stalled Haiku call must not block the caller — this is why the
    orchestrator wraps `resolve_entities` in `asyncio.wait_for`. If the
    resolver ever grows its own internal timeout, this test still documents
    the expectation: a caller-side `wait_for` cleanly cancels the work.
    """

    @pytest.mark.asyncio
    async def test_wait_for_cancels_stalled_resolver(self):
        stalling_adapter = _StallingAdapter()
        tenant_id = uuid.uuid4()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                TenantEntityResolver.resolve_entities(
                    user_message="test",
                    tenant_id=tenant_id,
                    db=None,  # unreachable: adapter stalls before any DB query
                    adapter=stalling_adapter,
                    model="claude-haiku-4-5-20251001",
                ),
                timeout=0.1,
            )

    @pytest.mark.asyncio
    async def test_gather_captures_resolver_timeout_as_exception(self):
        """asyncio.gather(return_exceptions=True) must capture TimeoutError
        so the orchestrator can branch on it instead of crashing the turn."""
        stalling_adapter = _StallingAdapter()
        tenant_id = uuid.uuid4()

        async def _fast_sibling() -> str:
            return "ok"

        results = await asyncio.gather(
            asyncio.wait_for(
                TenantEntityResolver.resolve_entities(
                    user_message="test",
                    tenant_id=tenant_id,
                    db=None,
                    adapter=stalling_adapter,
                    model="claude-haiku-4-5-20251001",
                ),
                timeout=0.1,
            ),
            _fast_sibling(),
            return_exceptions=True,
        )

        assert isinstance(results[0], asyncio.TimeoutError)
        assert results[1] == "ok"  # sibling task unaffected


class TestResolverTimeoutConstant:
    """Pin the orchestrator's resolver timeout well under the 300s chat budget."""

    def test_orchestrator_exposes_resolve_entities_timeout(self):
        from app.services.chat.orchestrator import _RESOLVE_ENTITIES_TIMEOUT_SECONDS

        assert 0 < _RESOLVE_ENTITIES_TIMEOUT_SECONDS <= 60, (
            f"_RESOLVE_ENTITIES_TIMEOUT_SECONDS={_RESOLVE_ENTITIES_TIMEOUT_SECONDS} "
            "is outside the interactive-chat band. Keep it <=60s so a stalled "
            "optional pre-flight call can never eat the 300s chat budget."
        )
