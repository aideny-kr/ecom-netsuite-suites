"""Test prompt trim — verify critical rules preserved and size reduced.

Phase 2 (2026-04-16) moved the SuiteQL dialect block from _SYSTEM_PROMPT
into knowledge_profiles/netsuite.yaml. Rule-content assertions below now
read the netsuite profile's prompt_fragment; size/shape assertions still
read _SYSTEM_PROMPT.
"""

import pytest

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT, UnifiedAgent
from app.services.chat.knowledge_profiles.loader import load_all_profiles


@pytest.fixture(scope="module")
def suiteql_prompt() -> str:
    """Combined base prompt + netsuite profile prompt_fragment.

    Rules that moved into netsuite.yaml live here after Phase 2. Rules
    that stayed in _SYSTEM_PROMPT (tool_selection, common_queries, etc.)
    are still findable since this fixture concatenates both.
    """
    profiles = load_all_profiles()
    netsuite = next((p for p in profiles if p.profile_id == "netsuite"), None)
    assert netsuite is not None
    return _SYSTEM_PROMPT + "\n" + netsuite.prompt_fragment


class TestPromptSize:
    def test_prompt_under_350_lines(self):
        """Trimmed prompt should be under 350 lines (was ~508)."""
        line_count = _SYSTEM_PROMPT.count("\n")
        assert line_count < 350, f"Prompt is {line_count} lines, target is <350"

    def test_prompt_under_13000_chars(self):
        """Trimmed prompt should be under 13000 chars.

        Phase 2 (2026-04-16) moved the ~148-line SuiteQL dialect block
        (~6000 chars) out of _SYSTEM_PROMPT into
        knowledge_profiles/netsuite.yaml's prompt_fragment. The base
        prompt now ships to every tenant; the SuiteQL rules only inject
        when a NetSuite read tool is in the turn's toolset.

        The 13000 ceiling is a leading indicator — when it trips, audit
        additions for value vs. token cost. Non-NS tenants should never
        pay the NS-rules tax.

        History:
        - Pre-2026-04-16: 18000 ceiling (all rules universal).
        - Phase 1 (2026-04-16): bumped to 18500 for ADDRESS TABLES block.
        - Phase 2 (2026-04-16): tightened to 13000 after SuiteQL move.
        """
        char_count = len(_SYSTEM_PROMPT)
        assert char_count < 13000, f"Prompt is {char_count} chars, target is <13000"


class TestCriticalRulesPreserved:
    """Every battle-tested rule must survive the trim."""

    def test_pagination_fetch_first(self, suiteql_prompt):
        assert "FETCH FIRST" in suiteql_prompt
        assert "ROWNUM" in suiteql_prompt

    def test_date_functions(self, suiteql_prompt):
        assert "TRUNC(SYSDATE)" in suiteql_prompt
        assert "BUILTIN.RELATIVE_RANGES" in suiteql_prompt
        assert "BUILTIN.DATE(SYSDATE)" in suiteql_prompt

    def test_boolean_fields(self, suiteql_prompt):
        assert "'T'" in suiteql_prompt
        assert "'F'" in suiteql_prompt

    def test_status_codes_single_letter(self, suiteql_prompt):
        assert "SalesOrd:B" in suiteql_prompt
        assert "single-letter" in suiteql_prompt.lower()

    def test_header_vs_line_aggregation(self, suiteql_prompt):
        assert "foreigntotal" in suiteql_prompt
        assert "HEADER-LEVEL" in suiteql_prompt

    def test_inventory_table(self, suiteql_prompt):
        assert "inventoryitemlocations" in suiteql_prompt

    def test_custom_list_fields(self, suiteql_prompt):
        assert "BUILTIN.DF" in suiteql_prompt

    def test_line_amount_sign(self, suiteql_prompt):
        assert "tl.foreignamount" in suiteql_prompt
        assert "* -1" in suiteql_prompt or "NEGATE" in suiteql_prompt.upper()

    def test_transaction_type_double_counting(self, suiteql_prompt):
        assert "double-count" in suiteql_prompt.lower() or "DOUBLE-COUNTING" in suiteql_prompt

    def test_multi_currency(self, suiteql_prompt):
        assert "t.total" in suiteql_prompt
        assert "base currency" in suiteql_prompt.lower()

    def test_item_table_safe_columns(self, suiteql_prompt):
        assert "itemid" in suiteql_prompt
        assert "displayname" in suiteql_prompt

    def test_assembly_component_filter(self, suiteql_prompt):
        assert "assemblycomponent" in suiteql_prompt

    def test_restricted_columns(self, suiteql_prompt):
        assert "tl.itemtype" in suiteql_prompt

    def test_createdfrom_chain(self, suiteql_prompt):
        assert "createdfrom" in suiteql_prompt

    def test_financial_aggregation_rule(self, suiteql_prompt):
        assert "GROUP BY" in suiteql_prompt
        assert "SUM" in suiteql_prompt

    def test_all_transaction_status_codes_present(self, suiteql_prompt):
        for tx_type in ["SalesOrd", "PurchOrd", "RtnAuth", "CustInvc", "ItemRcpt"]:
            assert tx_type in suiteql_prompt, f"Missing status codes for {tx_type}"

    def test_preflight_schema_check(self, suiteql_prompt):
        # tenant_schema is still in _SYSTEM_PROMPT (not moved) — but the fixture
        # concatenates both, so this test is indifferent.
        assert "tenant_schema" in suiteql_prompt


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
        assert agent.max_steps == 40


class TestDiagnoseBeforeSwitching:
    def test_diagnose_rule_in_unified_prompt(self):
        assert "diagnose" in _SYSTEM_PROMPT.lower()


class TestCompactionContinuation:
    def test_continuation_constant_exists(self):
        from app.services.chat.history_compactor import COMPACTION_CONTINUATION

        assert "resume" in COMPACTION_CONTINUATION.lower() or "do not recap" in COMPACTION_CONTINUATION.lower()
