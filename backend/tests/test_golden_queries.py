"""Golden test suite — intent classification + SuiteQL query accuracy.

Tests the heuristic intent classifier and validates that the SuiteQL agent
produces correct tool calls for common enterprise questions.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.coordinator import IntentType, classify_intent
from app.services.chat.agents.base_agent import AgentResult
from app.services.chat.agents.suiteql_agent import SuiteQLAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock


# ---------------------------------------------------------------------------
# Phase 1: Intent Classification — heuristic accuracy
# ---------------------------------------------------------------------------

# (question, expected_intent)
GOLDEN_INTENT_CASES = [
    # --- DATA_QUERY ---
    ("show me the latest 10 sales orders", IntentType.DATA_QUERY),
    ("how many invoices were created today", IntentType.DATA_QUERY),
    ("find customer Acme Corp", IntentType.DATA_QUERY),
    ("get the details for SO865732", IntentType.DATA_QUERY),
    ("pull the last 5 purchase orders", IntentType.DATA_QUERY),
    ("total sales this month", IntentType.DATA_QUERY),
    ("revenue by subsidiary last quarter", IntentType.DATA_QUERY),
    ("what's the balance due for vendor bills", IntentType.DATA_QUERY),
    ("inventory levels for FRAFMK0006", IntentType.DATA_QUERY),
    ("RMA61214", IntentType.DATA_QUERY),
    ("#12345", IntentType.DATA_QUERY),
    ("show me today's sales orders", IntentType.DATA_QUERY),
    ("sales order number 123456", IntentType.DATA_QUERY),
    # --- DOCUMENTATION ---
    ("how do I create a saved search in NetSuite", IntentType.DOCUMENTATION),
    ("what is SuiteQL syntax for date functions", IntentType.DOCUMENTATION),
    ("explain the error INVALID_SEARCH", IntentType.DOCUMENTATION),
    ("what tables are available in SuiteQL", IntentType.DOCUMENTATION),
    ("netsuite api reference for record types", IntentType.DOCUMENTATION),
    ("how can I use BUILTIN.DF in SuiteQL", IntentType.DOCUMENTATION),
    ("what does the N/record module do", IntentType.DOCUMENTATION),
    ("governance limit for map/reduce scripts", IntentType.WORKSPACE_DEV),  # "map/reduce" triggers workspace
    ("SuiteScript documentation for search.create", IntentType.DOCUMENTATION),
    ("what is a record type in NetSuite", IntentType.DOCUMENTATION),
    # --- WORKSPACE_DEV ---
    ("write a suitescript for user event", IntentType.WORKSPACE_DEV),
    ("create a restlet to handle file uploads", IntentType.WORKSPACE_DEV),
    ("review the code in ecom_file_cabinet_restlet.js", IntentType.WORKSPACE_DEV),
    ("refactor the scheduled script for inventory sync", IntentType.WORKSPACE_DEV),
    ("list files in the workspace", IntentType.WORKSPACE_DEV),
    ("search the codebase for afterSubmit", IntentType.WORKSPACE_DEV),
    ("write a jest test for the map/reduce script", IntentType.WORKSPACE_DEV),
    ("propose a patch for the client script", IntentType.WORKSPACE_DEV),
    # --- ANALYSIS ---
    ("compare sales Q1 2025 vs Q1 2026", IntentType.ANALYSIS),
    ("month-over-month revenue trend", IntentType.ANALYSIS),
    ("year-over-year growth rate for all subsidiaries", IntentType.ANALYSIS),
    ("top 10 customers by revenue", IntentType.ANALYSIS),
    ("analyze the breakdown of sales by platform", IntentType.ANALYSIS),
    # --- CODE_UNDERSTANDING ---
    ("how does the script calculate shipping cost", IntentType.CODE_UNDERSTANDING),
    ("where in the code is the tax logic", IntentType.CODE_UNDERSTANDING),
    ("what does the script do for inventory adjustments", IntentType.CODE_UNDERSTANDING),
    ("how are we calculating the discount percentage", IntentType.CODE_UNDERSTANDING),
]


class TestIntentClassification:
    @pytest.mark.parametrize("question,expected_intent", GOLDEN_INTENT_CASES)
    def test_heuristic_classification(self, question: str, expected_intent: IntentType):
        result = classify_intent(question)
        assert result == expected_intent, (
            f"Question: '{question}'\n"
            f"Expected: {expected_intent.value}, Got: {result.value}"
        )

    def test_ambiguous_falls_through(self):
        """Questions with no clear pattern should return AMBIGUOUS."""
        ambiguous = [
            "hello",
            "thanks",
            "can you help me",
            "what should I do next",
        ]
        for q in ambiguous:
            result = classify_intent(q)
            assert result == IntentType.AMBIGUOUS, (
                f"Expected AMBIGUOUS for '{q}', got {result.value}"
            )

    def test_numeric_id_shortcut(self):
        """Bare numeric IDs should route to DATA_QUERY."""
        assert classify_intent("12345") == IntentType.DATA_QUERY
        assert classify_intent("#99999") == IntentType.DATA_QUERY

    def test_transaction_prefix_shortcut(self):
        """Transaction prefixes (SO, PO, INV, etc.) should route to DATA_QUERY."""
        for prefix in ["SO865732", "INV12345", "PO99999", "RMA61214", "VB54321"]:
            result = classify_intent(prefix)
            assert result == IntentType.DATA_QUERY, (
                f"Expected DATA_QUERY for '{prefix}', got {result.value}"
            )


# ---------------------------------------------------------------------------
# Phase 2: SuiteQL Agent — query correctness
# ---------------------------------------------------------------------------


def _make_tool_use(tool_name: str, tool_input: dict) -> ToolUseBlock:
    return ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=tool_name, input=tool_input)


def _make_llm_response(
    text: str = "",
    tool_calls: list[ToolUseBlock] | None = None,
) -> LLMResponse:
    return LLMResponse(
        text_blocks=[text] if text else [],
        tool_use_blocks=tool_calls or [],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


def _make_text_response(text: str) -> LLMResponse:
    return _make_llm_response(text=text)


class TestSuiteQLQueryAccuracy:
    """Verify the SuiteQL agent produces correct tool calls for common questions.

    These tests mock the LLM adapter and verify that the agent correctly
    forwards the tool calls to execution. They test the full agent loop.
    """

    @pytest.fixture
    def mock_adapter(self):
        adapter = MagicMock()
        adapter.create_message = AsyncMock()
        adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})
        return adapter

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        return db

    @pytest.fixture
    def agent(self):
        return SuiteQLAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test-corr-id",
        )

    @pytest.mark.asyncio
    async def test_latest_sales_orders(self, agent, mock_adapter, mock_db):
        """'latest 10 sales orders' should use ORDER BY ... DESC FETCH FIRST 10 ROWS ONLY."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, t.foreigntotal FROM transaction t WHERE t.type = 'SalesOrd' ORDER BY t.id DESC FETCH FIRST 10 ROWS ONLY"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Here are the latest 10 sales orders."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["id"], "rows": [["1"]], "row_count": 1})
            result = await agent.run("show me the latest 10 sales orders", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        assert len(result.tool_calls_log) == 1
        call_params = result.tool_calls_log[0]["params"]
        query = call_params["query"].upper()
        assert "ORDER BY" in query
        assert "FETCH FIRST" in query
        assert "SALESORD" in query
        # Must NOT use ROWNUM with ORDER BY
        assert "ROWNUM" not in query

    @pytest.mark.asyncio
    async def test_revenue_aggregation_no_line_join(self, agent, mock_adapter, mock_db):
        """'total revenue today' should use SUM(t.foreigntotal) WITHOUT joining transactionline."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT COUNT(*) as order_count, SUM(t.foreigntotal) as total FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE)"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Total revenue today: $50,000"),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["order_count", "total"], "rows": [["10", "50000"]], "row_count": 1})
            result = await agent.run("total revenue today", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        # Should NOT join transactionline when using header-level totals
        assert "TRANSACTIONLINE" not in query
        assert "SUM" in query

    @pytest.mark.asyncio
    async def test_line_level_uses_foreignamount(self, agent, mock_adapter, mock_db):
        """When querying line-level data, should use tl.foreignamount, not t.foreigntotal."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT BUILTIN.DF(i.displayname) as item, SUM(tl.foreignamount * -1) as revenue FROM transactionline tl JOIN transaction t ON tl.transaction = t.id JOIN item i ON tl.item = i.id WHERE t.type = 'SalesOrd' AND tl.mainline = 'F' AND tl.taxline = 'F' GROUP BY BUILTIN.DF(i.displayname) ORDER BY revenue DESC FETCH FIRST 10 ROWS ONLY"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Top 10 items by revenue."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["item", "revenue"], "rows": [["Widget", "5000"]], "row_count": 1})
            result = await agent.run("top 10 items by revenue", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        # When joining transactionline, must use line-level amount
        assert "FOREIGNAMOUNT" in query
        # Must NOT use t.foreigntotal with transactionline join
        assert "FOREIGNTOTAL" not in query

    @pytest.mark.asyncio
    async def test_customer_lookup_by_name(self, agent, mock_adapter, mock_db):
        """Customer lookup should use LOWER(companyname) LIKE pattern."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT id, companyname, email FROM customer WHERE LOWER(companyname) LIKE '%acme%'"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Found customer Acme Corp."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["id", "companyname", "email"], "rows": [["1", "Acme Corp", "acme@test.com"]], "row_count": 1})
            result = await agent.run("find customer Acme Corp", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        assert "CUSTOMER" in query
        assert "LIKE" in query

    @pytest.mark.asyncio
    async def test_transaction_by_id_direct_lookup(self, agent, mock_adapter, mock_db):
        """Transaction number lookup should use WHERE t.tranid = 'RMA61214'."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, BUILTIN.DF(t.status) as status, t.foreigntotal FROM transaction t WHERE t.tranid = 'RMA61214'"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Found RMA61214."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["id", "tranid"], "rows": [["1", "RMA61214"]], "row_count": 1})
            result = await agent.run("look up RMA61214", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"]
        assert "RMA61214" in query

    @pytest.mark.asyncio
    async def test_multi_currency_uses_base_for_usd(self, agent, mock_adapter, mock_db):
        """'total in USD' should use SUM(t.total) which is already in base currency."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT SUM(t.total) as total_usd FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate >= TRUNC(SYSDATE) - 30"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Total in USD: $150,000"),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["total_usd"], "rows": [["150000"]], "row_count": 1})
            result = await agent.run("what's total sales in USD this month", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        # For USD totals, should use t.total (base currency), not t.foreigntotal
        assert "T.TOTAL" in query

    @pytest.mark.asyncio
    async def test_inventory_uses_inventoryitemlocations(self, agent, mock_adapter, mock_db):
        """Inventory queries should use inventoryitemlocations table."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT i.itemid, BUILTIN.DF(iil.location) as location, iil.quantityonhand, iil.quantityavailable FROM inventoryitemlocations iil JOIN item i ON iil.item = i.id WHERE LOWER(i.itemid) LIKE '%frafmk0006%' AND iil.quantityonhand != 0"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Inventory for FRAFMK0006."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["itemid", "location", "quantityonhand"], "rows": [["FRAFMK0006", "Main Warehouse", "50"]], "row_count": 1})
            result = await agent.run("inventory for FRAFMK0006", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        assert "INVENTORYITEMLOCATIONS" in query
        # Should NOT use inventorybalance (often restricted)
        assert "INVENTORYBALANCE" not in query

    @pytest.mark.asyncio
    async def test_item_lookup_uses_safe_columns(self, agent, mock_adapter, mock_db):
        """Item lookups should only use safe columns: id, itemid, displayname, description."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT i.id, i.itemid, i.displayname, i.description FROM item i WHERE LOWER(i.itemid) LIKE '%widget%'"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Found items matching 'widget'."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["id", "itemid"], "rows": [["1", "WIDGET-001"]], "row_count": 1})
            result = await agent.run("find item widget", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        assert "ITEM" in query
        # Should NOT use risky columns like itemtype, class, baseprice on item table
        assert "ITEMTYPE" not in query
        assert "BASEPRICE" not in query

    @pytest.mark.asyncio
    async def test_no_limit_keyword(self, agent, mock_adapter, mock_db):
        """SuiteQL does not support LIMIT — must use FETCH FIRST N ROWS ONLY."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT id, tranid FROM transaction WHERE type = 'SalesOrd' ORDER BY id DESC FETCH FIRST 5 ROWS ONLY"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Here are the orders."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["id", "tranid"], "rows": [["1", "SO001"]], "row_count": 1})
            result = await agent.run("show 5 recent orders", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        # Must use FETCH FIRST, never LIMIT
        assert "FETCH FIRST" in query
        assert " LIMIT " not in query

    @pytest.mark.asyncio
    async def test_no_double_counting_transaction_types(self, agent, mock_adapter, mock_db):
        """Sales queries should NOT filter on multiple transaction types that double-count."""
        suiteql_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT COUNT(*) as order_count, SUM(t.foreigntotal) as total FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate >= TRUNC(SYSDATE) - 30"
        })
        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[suiteql_call]),
            _make_text_response("Sales this month."),
        ]

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            mock_exec.return_value = json.dumps({"columns": ["order_count", "total"], "rows": [["100", "500000"]], "row_count": 1})
            result = await agent.run("total sales this month", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        query = result.tool_calls_log[0]["params"]["query"].upper()
        # Should NOT have IN ('SALESORD', 'CUSTINVC') which double-counts
        assert "CUSTINVC" not in query or "SALESORD" not in query  # Only one type

    @pytest.mark.asyncio
    async def test_error_recovery_metadata_lookup(self, agent, mock_adapter, mock_db):
        """When a query fails with 'Unknown identifier', agent should recover via metadata."""
        # Step 1: First attempt fails
        bad_query_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT id, itemid, itemtype FROM item WHERE ROWNUM <= 5"
        })
        # Step 2: Agent uses metadata to discover columns
        metadata_call = _make_tool_use("netsuite_get_metadata", {
            "record_type": "item"
        })
        # Step 3: Corrected query
        fixed_query_call = _make_tool_use("netsuite_suiteql", {
            "query": "SELECT id, itemid, displayname FROM item WHERE ROWNUM <= 5"
        })

        mock_adapter.create_message.side_effect = [
            _make_llm_response(tool_calls=[bad_query_call]),
            _make_llm_response(tool_calls=[metadata_call]),
            _make_llm_response(tool_calls=[fixed_query_call]),
            _make_text_response("Found 5 items."),
        ]

        call_count = 0

        async def mock_exec(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps({"error": True, "message": "Unknown identifier: itemtype"})
            elif call_count == 2:
                return json.dumps({"columns": ["id", "itemid", "displayname"], "metadata": True})
            else:
                return json.dumps({"columns": ["id", "itemid", "displayname"], "rows": [["1", "ITEM-001", "Widget"]], "row_count": 1})

        with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock, side_effect=mock_exec), \
             patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
            result = await agent.run("show me 5 items", {}, mock_db, mock_adapter, "claude-sonnet-4-5-20250929")

        assert result.success
        # Should have made 3 tool calls: bad query → metadata → fixed query
        assert len(result.tool_calls_log) == 3
        assert result.tool_calls_log[1]["tool"] == "netsuite_get_metadata"


# ---------------------------------------------------------------------------
# Phase 3: Edge cases — inject_fetch_limit, date functions, pagination
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_date_functions_in_prompt(self):
        """Verify the system prompt contains correct date function guidance."""
        agent = SuiteQLAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        assert "TRUNC(SYSDATE)" in prompt
        assert "CURRENT_DATE" in prompt  # Warning not to use it
        assert "BUILTIN.DATE(SYSDATE)" in prompt  # Warning not to use it

    def test_preflight_check_in_prompt(self):
        """Verify the preflight schema check guidance is in the prompt."""
        agent = SuiteQLAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        assert "PREFLIGHT SCHEMA CHECK" in prompt

    def test_prompt_contains_anti_hallucination(self):
        """The system prompt must have the anti-hallucination guard."""
        agent = SuiteQLAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        assert "ANTI-HALLUCINATION" in prompt
        assert "STRICTLY FORBIDDEN" in prompt

    def test_prompt_contains_inventory_guidance(self):
        """The system prompt must include inventoryitemlocations guidance."""
        agent = SuiteQLAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        assert "inventoryitemlocations" in prompt

    def test_web_search_tool_available(self):
        """SuiteQL agent should have web_search in its tool set."""
        from app.services.chat.agents.suiteql_agent import _SUITEQL_TOOL_NAMES
        assert "web_search" in _SUITEQL_TOOL_NAMES
        assert "netsuite_suiteql" in _SUITEQL_TOOL_NAMES
        assert "netsuite_get_metadata" in _SUITEQL_TOOL_NAMES
        assert "rag_search" in _SUITEQL_TOOL_NAMES

    def test_max_result_rows_cap(self):
        """Verify the agent caps results at _MAX_RESULT_ROWS."""
        from app.services.chat.agents.base_agent import _truncate_tool_result, _MAX_RESULT_ROWS

        big_result = json.dumps({
            "columns": ["id"],
            "rows": [[str(i)] for i in range(200)],
            "row_count": 200,
        })
        truncated = _truncate_tool_result(big_result)
        parsed = json.loads(truncated)
        assert len(parsed["rows"]) == _MAX_RESULT_ROWS
        assert parsed["rows_truncated"] is True
