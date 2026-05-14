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


class TestCurrentDatetimeIsDynamic:
    """The CURRENT DATE & TIME block contains HH:MM and changes every minute.
    It MUST land in the dynamic part so the static cache prefix stays stable.

    Codex review of the cache audit caught this — without these tests, the
    block silently leaked into the cached static prefix and invalidated the
    cache every minute, costing real money in cache_creation tokens.
    """

    def test_current_datetime_block_extracted_to_dynamic(self):
        prompt = (
            "You are an assistant.\n\n"
            "<current_datetime>\n## CURRENT DATE & TIME\n"
            "Timezone: America/Los_Angeles. Today: 2026-05-13, local time: 22:14.\n"
            "</current_datetime>\n\n"
            "Always include confidence tags."
        )
        result = split_system_prompt(prompt)
        assert "22:14" not in result.static
        assert "Today: 2026-05-13" not in result.static
        assert "## CURRENT DATE & TIME" not in result.static
        assert "<current_datetime>" in result.dynamic
        assert "22:14" in result.dynamic

    def test_static_stable_across_minute_change(self):
        """Same prompt with only HH:MM changed must yield IDENTICAL static.

        Without this fix, the static block changed every minute → cache invalidates
        every minute → users pay full price on every chat turn until cache rebuilds.
        """
        prompt_22_14 = (
            "You are an assistant.\n\n"
            "<current_datetime>\n## CURRENT DATE & TIME\n"
            "Today: 2026-05-13, local time: 22:14.\n"
            "</current_datetime>\n\n"
            "Always confidence."
        )
        prompt_22_15 = (
            "You are an assistant.\n\n"
            "<current_datetime>\n## CURRENT DATE & TIME\n"
            "Today: 2026-05-13, local time: 22:15.\n"
            "</current_datetime>\n\n"
            "Always confidence."
        )
        parts_a = split_system_prompt(prompt_22_14)
        parts_b = split_system_prompt(prompt_22_15)
        assert parts_a.static == parts_b.static
        assert parts_a.dynamic != parts_b.dynamic


class TestLearnedRulesIsDynamic:
    """Query-aware tenant-specific learned rules are rebuilt per-turn.
    They MUST be in dynamic, not static.

    Codex review caught: `<learned_rules>` was being injected into the
    static system prompt by both UnifiedAgent and BaseAgent, but the
    dynamic regex didn't match it. Each turn with different rules
    invalidated the static cache.
    """

    def test_learned_rules_block_extracted_to_dynamic(self):
        prompt = (
            "You are an assistant.\n\n"
            "<learned_rules>\nTenant-specific business rules — FOLLOW THESE STRICTLY:\n"
            "- When item.itemtype = 'Assembly', join bom on b.id = item.bomid.\n"
            "</learned_rules>\n\n"
            "End of prompt."
        )
        result = split_system_prompt(prompt)
        assert "learned_rules" not in result.static
        assert "Assembly" not in result.static
        assert "bomid" not in result.static
        assert "<learned_rules>" in result.dynamic
        assert "Assembly" in result.dynamic

    def test_static_stable_across_different_learned_rules(self):
        rules_a = (
            "You are an assistant.\n\n"
            "<learned_rules>\nRule set A: status codes are single-letter\n</learned_rules>\n\n"
            "End."
        )
        rules_b = (
            "You are an assistant.\n\n<learned_rules>\nRule set B: use BUILTIN.CONSOLIDATE\n</learned_rules>\n\nEnd."
        )
        parts_a = split_system_prompt(rules_a)
        parts_b = split_system_prompt(rules_b)
        assert parts_a.static == parts_b.static
        assert parts_a.dynamic != parts_b.dynamic
