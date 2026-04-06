"""Test prompt trim — verify critical rules preserved and size reduced."""

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT, UnifiedAgent


class TestPromptSize:
    def test_prompt_under_350_lines(self):
        """Trimmed prompt should be under 350 lines (was ~508)."""
        line_count = _SYSTEM_PROMPT.count("\n")
        assert line_count < 350, f"Prompt is {line_count} lines, target is <350"

    def test_prompt_under_18000_chars(self):
        """Trimmed prompt should be under 18000 chars (dialect rules alone = ~11500)."""
        char_count = len(_SYSTEM_PROMPT)
        assert char_count < 18000, f"Prompt is {char_count} chars, target is <18000"


class TestCriticalRulesPreserved:
    """Every battle-tested rule must survive the trim."""

    def test_pagination_fetch_first(self):
        assert "FETCH FIRST" in _SYSTEM_PROMPT
        assert "ROWNUM" in _SYSTEM_PROMPT

    def test_date_functions(self):
        assert "TRUNC(SYSDATE)" in _SYSTEM_PROMPT
        assert "BUILTIN.RELATIVE_RANGES" in _SYSTEM_PROMPT
        assert "BUILTIN.DATE(SYSDATE)" in _SYSTEM_PROMPT

    def test_boolean_fields(self):
        assert "'T'" in _SYSTEM_PROMPT
        assert "'F'" in _SYSTEM_PROMPT

    def test_status_codes_single_letter(self):
        assert "SalesOrd:B" in _SYSTEM_PROMPT
        assert "single-letter" in _SYSTEM_PROMPT.lower()

    def test_header_vs_line_aggregation(self):
        assert "foreigntotal" in _SYSTEM_PROMPT
        assert "HEADER-LEVEL" in _SYSTEM_PROMPT

    def test_inventory_table(self):
        assert "inventoryitemlocations" in _SYSTEM_PROMPT

    def test_custom_list_fields(self):
        assert "BUILTIN.DF" in _SYSTEM_PROMPT

    def test_line_amount_sign(self):
        assert "tl.foreignamount" in _SYSTEM_PROMPT
        assert "* -1" in _SYSTEM_PROMPT or "NEGATE" in _SYSTEM_PROMPT.upper()

    def test_transaction_type_double_counting(self):
        assert "double-count" in _SYSTEM_PROMPT.lower() or "DOUBLE-COUNTING" in _SYSTEM_PROMPT

    def test_multi_currency(self):
        assert "t.total" in _SYSTEM_PROMPT
        assert "base currency" in _SYSTEM_PROMPT.lower()

    def test_item_table_safe_columns(self):
        assert "itemid" in _SYSTEM_PROMPT
        assert "displayname" in _SYSTEM_PROMPT

    def test_assembly_component_filter(self):
        assert "assemblycomponent" in _SYSTEM_PROMPT

    def test_restricted_columns(self):
        assert "tl.itemtype" in _SYSTEM_PROMPT

    def test_createdfrom_chain(self):
        assert "createdfrom" in _SYSTEM_PROMPT

    def test_financial_aggregation_rule(self):
        assert "GROUP BY" in _SYSTEM_PROMPT
        assert "SUM" in _SYSTEM_PROMPT

    def test_all_transaction_status_codes_present(self):
        for tx_type in ["SalesOrd", "PurchOrd", "RtnAuth", "CustInvc", "ItemRcpt"]:
            assert tx_type in _SYSTEM_PROMPT, f"Missing status codes for {tx_type}"

    def test_preflight_schema_check(self):
        assert "tenant_schema" in _SYSTEM_PROMPT


class TestMaxSteps:
    def test_standard_max_steps_increased(self):
        agent = UnifiedAgent(
            tenant_id="00000000-0000-0000-0000-000000000000",
            user_id="00000000-0000-0000-0000-000000000000",
            correlation_id="test",
            context_need="data",
        )
        assert agent.max_steps == 12

    def test_investigation_max_steps_increased(self):
        agent = UnifiedAgent(
            tenant_id="00000000-0000-0000-0000-000000000000",
            user_id="00000000-0000-0000-0000-000000000000",
            correlation_id="test",
            context_need="full",
        )
        assert agent.max_steps == 15


class TestDiagnoseBeforeSwitching:
    def test_diagnose_rule_in_unified_prompt(self):
        assert "diagnose" in _SYSTEM_PROMPT.lower()


class TestCompactionContinuation:
    def test_continuation_constant_exists(self):
        from app.services.chat.history_compactor import COMPACTION_CONTINUATION
        assert "resume" in COMPACTION_CONTINUATION.lower() or "do not recap" in COMPACTION_CONTINUATION.lower()
