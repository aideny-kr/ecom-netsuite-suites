"""Tests for prompt cache — static/dynamic system prompt splitting."""

from app.services.chat.prompt_cache import StaticDynamicPrompt, split_system_prompt

SAMPLE_PROMPT = """You are a NetSuite specialist agent.

## Instructions
Follow these rules carefully when writing SuiteQL queries.

<tenant_vernacular>
Customer "Acme" maps to entity ID 12345.
"Widget" refers to item "WDG-100".
</tenant_vernacular>

## Schema
- transaction (id, type, status, entity)
- transactionline (transaction, item, amount)

<domain_knowledge>
Use BUILTIN.CONSOLIDATE for multi-currency reports.
Always join accountingperiod for date filtering.
</domain_knowledge>

## Output Format
Return results in markdown tables.

<proven_patterns>
SELECT t.id FROM transaction t WHERE t.type = 'SalesOrd'
</proven_patterns>

<financial_context>
Current period: 2026 Q1
Base currency: USD
</financial_context>

Always include confidence tags."""


PROMPT_NO_DYNAMIC = """You are a NetSuite specialist agent.

## Instructions
Follow these rules carefully.

## Schema
- transaction (id, type, status)

Always include confidence tags."""


class TestSplitSystemPrompt:
    def test_split_separates_static_and_dynamic(self):
        result = split_system_prompt(SAMPLE_PROMPT)

        assert isinstance(result, StaticDynamicPrompt)
        # Static should contain base content
        assert "You are a NetSuite specialist agent." in result.static
        assert "## Instructions" in result.static
        assert "## Schema" in result.static
        assert "## Output Format" in result.static
        assert "Always include confidence tags." in result.static

        # Dynamic should contain the XML blocks
        assert "<tenant_vernacular>" in result.dynamic
        assert "<domain_knowledge>" in result.dynamic
        assert "<proven_patterns>" in result.dynamic
        assert "<financial_context>" in result.dynamic

    def test_static_excludes_per_turn_context(self):
        result = split_system_prompt(SAMPLE_PROMPT)

        assert "<tenant_vernacular>" not in result.static
        assert "Acme" not in result.static
        assert "<domain_knowledge>" not in result.static
        assert "BUILTIN.CONSOLIDATE" not in result.static
        assert "<proven_patterns>" not in result.static
        assert "<financial_context>" not in result.static

    def test_empty_dynamic_returns_empty_string(self):
        result = split_system_prompt(PROMPT_NO_DYNAMIC)

        assert result.dynamic == ""
        assert "You are a NetSuite specialist agent." in result.static
        assert "## Instructions" in result.static

    def test_dynamic_preserves_block_content(self):
        result = split_system_prompt(SAMPLE_PROMPT)

        assert 'Customer "Acme" maps to entity ID 12345.' in result.dynamic
        assert "BUILTIN.CONSOLIDATE" in result.dynamic
        assert "SELECT t.id FROM transaction t" in result.dynamic
        assert "Current period: 2026 Q1" in result.dynamic

    def test_static_is_clean(self):
        """No leftover empty lines where blocks were removed."""
        result = split_system_prompt(SAMPLE_PROMPT)

        # Should not have 3+ consecutive newlines (indicates leftover gaps)
        assert "\n\n\n" not in result.static
