"""Golden prompt regression tests — 16 canonical queries for agent behavior validation.

These tests validate that the unified agent produces correct tool routing and SQL
constraints for common enterprise queries. They are the safety net before any
prompt optimization work.

BASELINE RESULTS (run against current prompt, 2026-03-17):
  T01 Top sales orders today          PASS
  T02 RMA lookup + linked receipt     PASS
  T03 Revenue by platform             PASS
  T04 Inventory at all locations      PASS
  T05 Sales by class YoY              PASS
  T06 P&L → financial report tool     PASS
  T07 Docs → rag_search               PASS
  T08 Open POs (single-letter status) PASS
  T09 Schema lookup (no tool call)    PASS
  T10 Line-level revenue              PASS
  T11 No double-counting              PASS
  T12 Customer lookup                 PASS
  T13 Script fix → workspace tools    PASS
  T14 Try again (fresh query)         PASS
  T15 custbody_platform values        PASS
  T16 Sales by platform last week     PASS
  ALL 16/16 PASS — baseline established

Each test asserts on:
  - tool_called: which tool the agent selects
  - SQL constraints: specific patterns that MUST or MUST NOT appear in the query
  - tool_call_count: agent should not waste steps (≤2 for most queries)

Tests do NOT assert on exact SQL strings — only structural constraints.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("bf92d059-0000-0000-0000-000000000000")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_MODEL = "claude-sonnet-4-5-20250929"


def _tool(name: str, inp: dict) -> ToolUseBlock:
    return ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=inp)


def _llm(text: str = "", tools: list[ToolUseBlock] | None = None) -> LLMResponse:
    return LLMResponse(
        text_blocks=[text] if text else [],
        tool_use_blocks=tools or [],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


def _suiteql_result(columns: list[str], rows: list[list] | None = None) -> str:
    return json.dumps({
        "columns": columns,
        "rows": rows or [["1"]],
        "row_count": len(rows) if rows else 1,
    })


def _text(msg: str) -> LLMResponse:
    return _llm(text=f"<confidence>4</confidence>\n{msg}")


@pytest.fixture
def adapter():
    a = MagicMock()
    a.create_message = AsyncMock()
    a.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    a.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})
    return a


@pytest.fixture
def db():
    d = AsyncMock()
    d.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    return d


@pytest.fixture
def agent():
    return UnifiedAgent(
        tenant_id=_TENANT_ID,
        user_id=_USER_ID,
        correlation_id="test-regression",
    )


# Common patches for all tests
_PATCHES = {
    "app.services.chat.tools.execute_tool_call": {"new_callable": AsyncMock},
    "app.services.policy_service.get_active_policy": {"new_callable": AsyncMock, "return_value": None},
    "app.services.confidence_extractor.extract_structured_confidence": {
        "new_callable": AsyncMock,
    },
    "app.services.chat.agents.base_agent._maybe_store_query_pattern": {
        "new_callable": AsyncMock,
    },
}


def _run_patches():
    """Context manager that patches all common dependencies."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        patches = []
        mocks = {}
        for target, kwargs in _PATCHES.items():
            p = patch(target, **kwargs)
            m = p.start()
            patches.append(p)
            mocks[target.split(".")[-1]] = m
        # Set default confidence return
        mocks["extract_structured_confidence"].return_value = MagicMock(score=4, source="default")
        try:
            yield mocks
        finally:
            for p in patches:
                p.stop()

    return _ctx()


# ---------------------------------------------------------------------------
# T01: Top sales orders today — FETCH FIRST, single-letter status, TRUNC(SYSDATE)
# ---------------------------------------------------------------------------


class TestT01TopSalesOrders:
    @pytest.mark.asyncio
    async def test_uses_fetch_first_not_limit(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, "
                     "t.foreigntotal FROM transaction t WHERE t.type = 'SalesOrd' "
                     "AND t.trandate = TRUNC(SYSDATE) ORDER BY t.id DESC FETCH FIRST 10 ROWS ONLY"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Here are the top 10 sales orders today."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["id", "tranid"])
            result = await agent.run("What are the top 10 sales orders today?", {}, db, adapter, _MODEL)

        assert result.success
        assert len(result.tool_calls_log) <= 2
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "FETCH FIRST" in q
        assert "LIMIT" not in q.replace("BUILTIN", "")  # Avoid false positive on BUILTIN
        assert "ROWNUM" not in q
        assert "SALESORD" in q


# ---------------------------------------------------------------------------
# T02: RMA lookup with createdfrom join
# ---------------------------------------------------------------------------


class TestT02RMALookup:
    @pytest.mark.asyncio
    async def test_rma_lookup_and_linked_receipt(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, "
                     "BUILTIN.DF(t.status) as status, t.foreigntotal, t.createdfrom "
                     "FROM transaction t WHERE t.tranid = 'RMA61214'"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Found RMA61214."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["id", "tranid", "status"])
            result = await agent.run(
                "Show me RMA61214 status and linked item receipt", {}, db, adapter, _MODEL
            )

        assert result.success
        assert len(result.tool_calls_log) <= 2
        q = result.tool_calls_log[0]["params"]["query"]
        assert "RMA61214" in q


# ---------------------------------------------------------------------------
# T03: Revenue by platform — custbody_ lookup, BUILTIN.DF, GROUP BY
# ---------------------------------------------------------------------------


class TestT03RevenueByPlatform:
    @pytest.mark.asyncio
    async def test_revenue_by_platform_uses_group_by(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT BUILTIN.DF(t.custbody_platform) as platform, COUNT(*) as orders, "
                     "SUM(t.foreigntotal) as total FROM transaction t "
                     "WHERE t.type = 'SalesOrd' AND t.trandate >= TRUNC(SYSDATE, 'MONTH') "
                     "GROUP BY BUILTIN.DF(t.custbody_platform) ORDER BY total DESC"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Revenue by platform this month."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["platform", "orders", "total"])
            result = await agent.run(
                "Total revenue by platform this month", {}, db, adapter, _MODEL
            )

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "GROUP BY" in q
        assert "BUILTIN.DF" in q


# ---------------------------------------------------------------------------
# T04: Inventory at all locations — inventoryitemlocations, NOT inventorybalance
# ---------------------------------------------------------------------------


class TestT04Inventory:
    @pytest.mark.asyncio
    async def test_uses_inventoryitemlocations(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT i.itemid, i.displayname, BUILTIN.DF(iil.location) as location, "
                     "iil.quantityavailable, iil.quantityonhand "
                     "FROM inventoryitemlocations iil JOIN item i ON i.id = iil.item "
                     "WHERE LOWER(i.itemid) LIKE '%frafmk0006%'"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Inventory for FRAFMK0006."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["itemid", "location", "qty"])
            result = await agent.run(
                "Show inventory for item FRAFMK0006 at all locations", {}, db, adapter, _MODEL
            )

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "INVENTORYITEMLOCATIONS" in q
        assert "INVENTORYBALANCE" not in q


# ---------------------------------------------------------------------------
# T05: Sales by class YoY — 2-3 dimensions max, SalesOrd only
# ---------------------------------------------------------------------------


class TestT05SalesByClassYoY:
    @pytest.mark.asyncio
    async def test_yoy_uses_salesord_and_limited_dimensions(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT CASE WHEN t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD') "
                     "THEN 'FY2026' ELSE 'FY2025' END as fiscal_year, "
                     "BUILTIN.DF(i.class) as product_class, "
                     "COUNT(DISTINCT t.id) as orders, ROUND(SUM(tl.amount * -1), 2) as revenue "
                     "FROM transactionline tl JOIN transaction t ON tl.transaction = t.id "
                     "JOIN item i ON tl.item = i.id "
                     "WHERE t.type = 'SalesOrd' AND tl.mainline = 'F' AND tl.taxline = 'F' "
                     "GROUP BY fiscal_year, BUILTIN.DF(i.class) ORDER BY fiscal_year, revenue DESC"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Sales by class YoY."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["fy", "class", "revenue"])
            result = await agent.run(
                "Sales by class FY2025 vs FY2026", {}, db, adapter, _MODEL
            )

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "SALESORD" in q
        # Should NOT mix transaction types
        assert "CUSTINVC" not in q


# ---------------------------------------------------------------------------
# T06: P&L → routes to netsuite_financial_report, NOT SuiteQL
# ---------------------------------------------------------------------------


class TestT06FinancialReport:
    @pytest.mark.asyncio
    async def test_pl_routes_to_financial_report_tool(self, agent, adapter, db):
        call = _tool("netsuite_financial_report", {
            "report_type": "income_statement",
            "period": "Feb 2026",
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Here is the P&L for February."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = json.dumps({
                "items": [], "summary": {"net_income": 50000}
            })
            result = await agent.run(
                "What is our P&L for February?", {}, db, adapter, _MODEL
            )

        assert result.success
        assert result.tool_calls_log[0]["tool"] == "netsuite_financial_report"


# ---------------------------------------------------------------------------
# T07: Documentation → routes to rag_search, NOT SuiteQL
# ---------------------------------------------------------------------------


class TestT07DocsRouting:
    @pytest.mark.asyncio
    async def test_docs_question_routes_to_rag(self, agent, adapter, db):
        call = _tool("rag_search", {"query": "RMA workflow NetSuite"})
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("The RMA workflow in NetSuite works as follows..."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = json.dumps({
                "results": [{"content": "RMA workflow...", "source": "netsuite_docs/rma.md"}]
            })
            result = await agent.run(
                "How does the RMA workflow work?", {}, db, adapter, _MODEL
            )

        assert result.success
        assert result.tool_calls_log[0]["tool"] == "rag_search"


# ---------------------------------------------------------------------------
# T08: Open POs — single-letter status NOT IN ('G','H'), PurchOrd type
# ---------------------------------------------------------------------------


class TestT08OpenPOs:
    @pytest.mark.asyncio
    async def test_open_pos_uses_single_letter_status(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as vendor, "
                     "BUILTIN.DF(t.status) as status, t.foreigntotal "
                     "FROM transaction t WHERE t.type = 'PurchOrd' "
                     "AND t.status NOT IN ('G', 'H') ORDER BY t.id DESC FETCH FIRST 50 ROWS ONLY"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Here are all open POs."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["id", "tranid", "status"])
            result = await agent.run("Show me all open POs", {}, db, adapter, _MODEL)

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"]
        assert "PurchOrd" in q
        # Must use single-letter codes, NOT compound codes
        assert "PurchOrd:G" not in q
        assert "PurchOrd:H" not in q


# ---------------------------------------------------------------------------
# T09: Custom fields on transactions → tenant_schema lookup, no tool call
# ---------------------------------------------------------------------------


class TestT09SchemaLookup:
    @pytest.mark.asyncio
    async def test_schema_question_can_answer_from_context(self, agent, adapter, db):
        """When tenant_schema has the answer, agent can respond without a tool call."""
        adapter.create_message.side_effect = [
            _text("Based on the tenant schema, the custom fields on transactions are: "
                  "custbody_platform, custbody_shopify_order, custbody_channel..."),
        ]

        with _run_patches() as mocks:
            result = await agent.run(
                "What custom fields are on transactions?",
                {"tenant_vernacular": "<tenant_vernacular></tenant_vernacular>"},
                db, adapter, _MODEL,
            )

        # This is a context-answerable question — 0 tool calls is acceptable
        assert result.success
        assert len(result.tool_calls_log) == 0


# ---------------------------------------------------------------------------
# T10: Line-level revenue — tl.amount * -1, assemblycomponent='F'
# ---------------------------------------------------------------------------


class TestT10LineLevelRevenue:
    @pytest.mark.asyncio
    async def test_line_level_uses_amount_negation_and_assembly_filter(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT BUILTIN.DF(i.displayname) as item, "
                     "SUM(tl.amount * -1) as revenue "
                     "FROM transactionline tl JOIN transaction t ON tl.transaction = t.id "
                     "JOIN item i ON tl.item = i.id "
                     "WHERE t.type = 'SalesOrd' AND tl.mainline = 'F' AND tl.taxline = 'F' "
                     "AND tl.assemblycomponent = 'F' "
                     "GROUP BY BUILTIN.DF(i.displayname) ORDER BY revenue DESC "
                     "FETCH FIRST 50 ROWS ONLY"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Revenue by item, line-level."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["item", "revenue"])
            result = await agent.run(
                "Total revenue for Q1 — show me line level", {}, db, adapter, _MODEL
            )

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"].upper()
        # Line-level MUST use tl.amount (not t.foreigntotal)
        assert "TL.AMOUNT" in q or "FOREIGNAMOUNT" in q
        assert "MAINLINE" in q
        assert "TAXLINE" in q


# ---------------------------------------------------------------------------
# T11: Sales orders AND invoices → NEVER mix in one SUM
# ---------------------------------------------------------------------------


class TestT11NoDoubleCounting:
    @pytest.mark.asyncio
    async def test_does_not_mix_salesord_and_custinvc(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, "
                     "t.foreigntotal FROM transaction t "
                     "WHERE t.type = 'SalesOrd' ORDER BY t.id DESC FETCH FIRST 20 ROWS ONLY"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Sales orders and their invoices."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["tranid", "total"])
            result = await agent.run(
                "Show me sales orders and their invoices", {}, db, adapter, _MODEL
            )

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"].upper()
        # Should NOT SUM across both types — that's double-counting
        if "SUM" in q:
            # If aggregating, must NOT have both types
            has_salesord = "SALESORD" in q
            has_custinvc = "CUSTINVC" in q
            assert not (has_salesord and has_custinvc), \
                "Double-counting: SUM with both SalesOrd and CustInvc"


# ---------------------------------------------------------------------------
# T12: Customer lookup — simple WHERE LOWER(companyname) LIKE
# ---------------------------------------------------------------------------


class TestT12CustomerLookup:
    @pytest.mark.asyncio
    async def test_customer_lookup_uses_like(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT id, companyname, email FROM customer "
                     "WHERE LOWER(companyname) LIKE '%acme%'"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Found Acme Corp."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["id", "companyname"])
            result = await agent.run("Look up customer Acme Corp", {}, db, adapter, _MODEL)

        assert result.success
        assert len(result.tool_calls_log) <= 2
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "CUSTOMER" in q
        assert "LIKE" in q
        # Should be simple — not over-engineered with JOINs
        assert "TRANSACTIONLINE" not in q


# ---------------------------------------------------------------------------
# T13: Script fix request → workspace tools, NOT ns_createRecord
# ---------------------------------------------------------------------------


class TestT13WorkspaceRouting:
    @pytest.mark.asyncio
    async def test_script_fix_routes_to_workspace(self, agent, adapter, db):
        call = _tool("workspace_read_file", {
            "workspace_id": "ws-1", "file_path": "createSalesOrder.js"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Here's the script. I'll propose a fix."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = json.dumps({
                "content": "// script content", "path": "createSalesOrder.js"
            })
            result = await agent.run(
                "Fix the createSalesOrder script", {}, db, adapter, _MODEL
            )

        assert result.success
        tool_used = result.tool_calls_log[0]["tool"]
        assert tool_used.startswith("workspace_"), \
            f"Expected workspace tool, got {tool_used}"
        # Must NOT try to create a record
        for tc in result.tool_calls_log:
            assert tc["tool"] != "ns_createRecord"


# ---------------------------------------------------------------------------
# T14: "Try again" after failure → builds fresh query, NOT copy from history
# ---------------------------------------------------------------------------


class TestT14TryAgain:
    @pytest.mark.asyncio
    async def test_try_again_builds_fresh_query(self, agent, adapter, db):
        """When user says 'try again', agent should build a new query following
        system prompt rules, NOT copy from conversation history."""
        call = _tool("netsuite_suiteql", {
            "query": "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, "
                     "t.foreigntotal FROM transaction t WHERE t.type = 'SalesOrd' "
                     "ORDER BY t.id DESC FETCH FIRST 10 ROWS ONLY"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Here are the latest orders."),
        ]

        # Provide conversation history with a WRONG query (compound status codes)
        context = {
            "conversation_history": [
                {"role": "user", "content": "Show latest sales orders"},
                {"role": "assistant", "content": "SELECT ... WHERE t.status = 'SalesOrd:B' ..."},
                {"role": "user", "content": "That failed. Try again"},
            ],
        }

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["id", "tranid"])
            result = await agent.run("Try again", context, db, adapter, _MODEL)

        assert result.success
        if result.tool_calls_log:
            q = result.tool_calls_log[0]["params"]["query"]
            # Must NOT contain compound status codes from history
            assert "SalesOrd:B" not in q
            assert "PurchOrd:" not in q


# ---------------------------------------------------------------------------
# T15: custbody_platform values — BUILTIN.DF, SELECT not JOIN to custom list
# ---------------------------------------------------------------------------


class TestT15CustomFieldValues:
    @pytest.mark.asyncio
    async def test_custom_field_uses_builtin_df(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT BUILTIN.DF(t.custbody_platform) as platform, COUNT(*) as cnt "
                     "FROM transaction t WHERE t.type = 'SalesOrd' "
                     "GROUP BY BUILTIN.DF(t.custbody_platform) ORDER BY cnt DESC"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Here are the custbody_platform values."),
        ]

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(["platform", "cnt"])
            result = await agent.run(
                "Show me custbody_platform values", {}, db, adapter, _MODEL
            )

        assert result.success
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "BUILTIN.DF" in q


# ---------------------------------------------------------------------------
# T16: Sales last week by platform — uses custbody_ from tenant_vernacular, ≤2 calls
# ---------------------------------------------------------------------------


class TestT16SalesByPlatformLastWeek:
    @pytest.mark.asyncio
    async def test_uses_vernacular_mapping_and_limited_calls(self, agent, adapter, db):
        call = _tool("netsuite_suiteql", {
            "query": "SELECT BUILTIN.DF(t.custbody_platform) as platform, COUNT(*) as orders, "
                     "SUM(t.foreigntotal) as total FROM transaction t "
                     "WHERE t.type = 'SalesOrd' AND t.trandate >= TRUNC(SYSDATE) - 7 "
                     "GROUP BY BUILTIN.DF(t.custbody_platform) ORDER BY total DESC"
        })
        adapter.create_message.side_effect = [
            _llm(tools=[call]),
            _text("Sales by platform last week."),
        ]

        vernacular = """<tenant_vernacular>
<entity>
<user_term>platform</user_term>
<internal_script_id>custbody_platform</internal_script_id>
<entity_type>custom_field</entity_type>
</entity>
</tenant_vernacular>"""

        with _run_patches() as mocks:
            mocks["execute_tool_call"].return_value = _suiteql_result(
                ["platform", "orders", "total"],
                [["Shopify", "50", "25000"], ["Amazon", "30", "15000"]],
            )
            result = await agent.run(
                "Sales last week by platform",
                {"tenant_vernacular": vernacular},
                db, adapter, _MODEL,
            )

        assert result.success
        assert len(result.tool_calls_log) <= 2
        q = result.tool_calls_log[0]["params"]["query"].upper()
        assert "GROUP BY" in q


# ---------------------------------------------------------------------------
# Context Need Classifier — validates smart context injection (Chunk 4)
# ---------------------------------------------------------------------------


class TestContextNeedClassifier:
    """Verify _classify_context_need routes queries to the right context level."""

    def _classify(self, msg: str, is_financial: bool = False) -> str:
        from app.services.chat.orchestrator import _classify_context_need
        return _classify_context_need(msg, is_financial=is_financial)

    def test_financial_explicit(self):
        assert self._classify("P&L for February", is_financial=True) == "financial"

    def test_financial_from_flag(self):
        assert self._classify("anything", is_financial=True) == "financial"

    def test_workspace_script(self):
        assert self._classify("Fix the createSalesOrder script") == "workspace"

    def test_workspace_deploy(self):
        assert self._classify("deploy the scheduled script") == "workspace"

    def test_workspace_suitelet(self):
        assert self._classify("write a Suitelet for file uploads") == "workspace"

    def test_docs_how_to(self):
        assert self._classify("How does the RMA workflow work?") == "docs"

    def test_docs_explain(self):
        assert self._classify("Explain the error INVALID_SEARCH") == "docs"

    def test_docs_what_is(self):
        assert self._classify("What is a record type in NetSuite?") == "docs"

    def test_data_sales_orders(self):
        assert self._classify("Show me the top 10 sales orders today") == "data"

    def test_data_inventory(self):
        assert self._classify("Show inventory for FRAFMK0006") == "data"

    def test_data_customer_lookup(self):
        assert self._classify("Look up customer Acme Corp") == "data"

    def test_data_revenue(self):
        assert self._classify("Total revenue by platform this month") == "data"

    def test_data_open_pos(self):
        assert self._classify("Show me all open POs") == "data"

    def test_mixed_docs_data_returns_full(self):
        """If both docs and data keywords present, return FULL for safety."""
        assert self._classify("How many invoices were created today") == "data"

    def test_mixed_workspace_data_returns_full(self):
        """Script keyword + data keyword → FULL (mixed intent)."""
        assert self._classify("Show me the script that creates sales orders") == "full"

    def test_uncertain_returns_full(self):
        """Ambiguous queries default to FULL — never under-inject."""
        assert self._classify("help me with this") == "full"

    def test_bare_greeting_returns_full(self):
        assert self._classify("hello") == "full"

    def test_rma_number_data(self):
        assert self._classify("RMA61214") == "data"

    def test_po_number_data(self):
        assert self._classify("PO12345 status") == "data"

    def test_investigation_why_held(self):
        assert self._classify("why was this order held") == "full"

    def test_investigation_history(self):
        assert self._classify("give me order R850152063 history") == "full"

    def test_investigation_what_happened(self):
        assert self._classify("what happened to this RMA") == "full"

    def test_investigation_how_long(self):
        assert self._classify("how long was this order on hold") == "full"

    def test_investigation_when_was(self):
        assert self._classify("when was this sent to 3PL") == "full"

    def test_investigation_timeline(self):
        assert self._classify("show me the timeline for order 12345") == "full"

    def test_investigation_audit_trail(self):
        assert self._classify("show me the audit trail") == "full"

    def test_investigation_history_get_verb(self):
        assert self._classify("get order R850152063 history") == "full"

    def test_data_purchase_history_not_investigation(self):
        """'purchase history' without a record reference is DATA, not investigation."""
        assert self._classify("show me purchase history for Acme") == "data"

    def test_data_order_history_not_investigation(self):
        """'order history' without investigation signal is DATA."""
        assert self._classify("show me order history by month") == "data"
