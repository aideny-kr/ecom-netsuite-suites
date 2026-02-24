"""Tests for tenant_resolver — NER extraction prompt."""

from app.services.chat.tenant_resolver import EXTRACTOR_SYSTEM_PROMPT


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
