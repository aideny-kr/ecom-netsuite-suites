"""Tests for tenant query pattern service — extraction, storage, retrieval, and confidence parsing."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_query_pattern import TenantQueryPattern
from app.services.chat.agents.base_agent import (
    _LOW_CONFIDENCE_DISCLAIMER,
    parse_confidence,
    strip_confidence_tag,
)
from app.services.query_pattern_service import (
    _extract_columns,
    _extract_tables,
    extract_and_store_pattern,
)
from tests.conftest import create_test_tenant

# ---------------------------------------------------------------------------
# Unit tests: confidence parsing
# ---------------------------------------------------------------------------


class TestConfidenceParsing:
    def test_parse_confidence_valid(self):
        assert parse_confidence("Here are the results. <confidence>5</confidence>") == 5
        assert parse_confidence("<confidence>1</confidence>") == 1
        assert parse_confidence("blah <confidence>3</confidence> more text") == 3

    def test_parse_confidence_missing(self):
        assert parse_confidence("No confidence tag here") is None
        assert parse_confidence("") is None

    def test_strip_confidence_tag(self):
        text = "Here are the results. <confidence>4</confidence>"
        result = strip_confidence_tag(text)
        assert "<confidence>" not in result
        assert "Here are the results." in result

    def test_low_confidence_disclaimer(self):
        """Confidence <= 2 should append a disclaimer."""
        assert "not fully confident" in _LOW_CONFIDENCE_DISCLAIMER


# ---------------------------------------------------------------------------
# Unit tests: SQL extraction helpers
# ---------------------------------------------------------------------------


class TestSQLExtraction:
    def test_extract_tables(self):
        sql = "SELECT t.id FROM transaction t JOIN transactionline tl ON tl.transaction = t.id"
        tables = _extract_tables(sql)
        assert "transaction" in tables
        assert "transactionline" in tables

    def test_extract_tables_case_insensitive(self):
        sql = "select id from CUSTOMER where companyname = 'Acme'"
        tables = _extract_tables(sql)
        assert "customer" in tables

    def test_extract_columns(self):
        sql = "SELECT t.id, t.tranid, tl.foreignamount FROM transaction t"
        columns = _extract_columns(sql)
        assert "t.id" in columns
        assert "t.tranid" in columns
        assert "tl.foreignamount" in columns

    def test_extract_columns_empty(self):
        sql = "SELECT 1"
        columns = _extract_columns(sql)
        assert columns == []


# ---------------------------------------------------------------------------
# Integration tests: pattern storage (require DB)
# ---------------------------------------------------------------------------


class TestPatternStorage:
    @pytest.mark.asyncio
    async def test_insert_pattern(self, db: AsyncSession):
        """extract_and_store_pattern should insert a new pattern."""
        tenant = await create_test_tenant(db, name="Pattern Corp")

        tool_calls_log = [
            {
                "tool": "netsuite_suiteql",
                "params": {
                    "query": "SELECT t.type, COUNT(t.id) as cnt FROM transaction t WHERE t.type = 'SalesOrd' GROUP BY t.type"
                },
                "result_summary": '{"columns": ["type", "cnt"], "rows": [["SalesOrd", "42"]], "row_count": 1}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(db, tenant.id, "how many sales orders do we have?", tool_calls_log)
            await db.flush()

        assert stored is True

        result = await db.execute(select(TenantQueryPattern).where(TenantQueryPattern.tenant_id == tenant.id))
        patterns = result.scalars().all()
        assert len(patterns) == 1
        assert patterns[0].user_question == "how many sales orders do we have?"
        assert "SalesOrd" in patterns[0].working_sql
        assert "transaction" in patterns[0].tables_used

    @pytest.mark.asyncio
    async def test_insert_pattern_from_result_payload(self, db: AsyncSession):
        """Structured result_payload should be enough to store a query pattern."""
        tenant = await create_test_tenant(db, name="Payload Corp")

        tool_calls_log = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT t.type, COUNT(t.id) as cnt FROM transaction t GROUP BY t.type"},
                "result_summary": "Returned 1 row",
                "result_payload": {
                    "kind": "table",
                    "columns": ["type", "cnt"],
                    "rows": [["SalesOrd", "42"]],
                    "row_count": 1,
                    "truncated": False,
                    "query": "SELECT t.type, COUNT(t.id) as cnt FROM transaction t GROUP BY t.type",
                    "limit": 100,
                },
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(db, tenant.id, "count sales orders by type", tool_calls_log)
            await db.flush()

        assert stored is True

        result = await db.execute(select(TenantQueryPattern).where(TenantQueryPattern.tenant_id == tenant.id))
        patterns = result.scalars().all()
        assert len(patterns) == 1
        assert "transaction" in patterns[0].tables_used

    @pytest.mark.asyncio
    async def test_upsert_increments_count(self, db: AsyncSession):
        """Running the same query twice should increment success_count."""
        tenant = await create_test_tenant(db, name="Upsert Corp")
        sql = "SELECT type, COUNT(id) as cnt FROM item GROUP BY type"

        tool_calls_log = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": sql},
                "result_summary": '{"columns": ["type", "cnt"], "rows": [["InvtPart", "5"]], "row_count": 1}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            await extract_and_store_pattern(db, tenant.id, "count items by type", tool_calls_log)
            await db.flush()
            await extract_and_store_pattern(db, tenant.id, "count items by type again", tool_calls_log)
            await db.flush()

        result = await db.execute(select(TenantQueryPattern).where(TenantQueryPattern.tenant_id == tenant.id))
        patterns = result.scalars().all()
        assert len(patterns) == 1
        assert patterns[0].success_count == 2

    @pytest.mark.asyncio
    async def test_skips_error_results(self, db: AsyncSession):
        """Should not store patterns from failed tool calls."""
        tenant = await create_test_tenant(db, name="Error Corp")

        tool_calls_log = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT bad_column FROM item"},
                "result_summary": '{"error": true, "message": "Unknown identifier: bad_column"}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(db, tenant.id, "bad query", tool_calls_log)

        assert stored is False

    @pytest.mark.asyncio
    async def test_skips_non_analytical_queries(self, db: AsyncSession):
        """Should not store simple probe queries without GROUP BY / aggregation."""
        tenant = await create_test_tenant(db, name="Probe Corp")

        tool_calls_log = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT id, itemid FROM item FETCH FIRST 5 ROWS ONLY"},
                "result_summary": '{"columns": ["id", "itemid"], "rows": [["1", "FW001"]], "row_count": 1}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(db, tenant.id, "show items", tool_calls_log)

        assert stored is False

    @pytest.mark.asyncio
    async def test_skips_zero_row_results(self, db: AsyncSession):
        """Should not store patterns from queries returning 0 rows."""
        tenant = await create_test_tenant(db, name="Empty Corp")

        tool_calls_log = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT type, COUNT(id) as cnt FROM transaction GROUP BY type"},
                "result_summary": '{"columns": ["type", "cnt"], "rows": [], "row_count": 0}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(db, tenant.id, "orders by type", tool_calls_log)

        assert stored is False

    @pytest.mark.asyncio
    async def test_stores_external_mcp_suiteql_pattern(self, db: AsyncSession):
        """External MCP ns_runCustomSuiteQL calls must also persist patterns.

        Regression for Olivia's 2026-04-09 session: the successful
        transactionShippingAddress join ran via ext__<hex>__ns_runCustomSuiteQL
        but never got stored because the extractor only matched the local
        `netsuite_suiteql` tool name. The follow-up turn had no cached
        pattern to fall back to and hallucinated the wrong answer.
        """
        tenant = await create_test_tenant(db, name="Ext MCP Corp")

        tool_calls_log = [
            {
                "tool": "ext__fc1cba33e9924f62a5b7df0d5f235214__ns_runCustomSuiteQL",
                "params": {
                    "sqlQuery": (
                        "SELECT BUILTIN.DF(sa.country) AS ship_country, "
                        "COUNT(DISTINCT t.id) AS total_orders, "
                        "SUM(ABS(tl.quantity)) AS total_qty "
                        "FROM transaction t "
                        "JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress "
                        "JOIN transactionline tl ON tl.transaction = t.id "
                        "WHERE t.type = 'SalesOrd' "
                        "GROUP BY BUILTIN.DF(sa.country)"
                    ),
                    "description": "Sales by ship country",
                },
                "result_summary": "Returned 4 rows",
                "result_payload": {
                    "kind": "table",
                    "columns": ["ship_country", "total_orders", "total_qty"],
                    "rows": [
                        ["Switzerland", 16, 146],
                        ["Norway", 3, 25],
                        ["Singapore", 3, 6],
                        ["New Zealand", 1, 3],
                    ],
                    "row_count": 4,
                    "truncated": False,
                    "query": "...",
                    "limit": 4,
                },
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(
                db,
                tenant.id,
                "give me sales data by shipping country for Norway, Switzerland, NZ, Singapore",
                tool_calls_log,
            )
            await db.flush()

        assert stored is True

        result = await db.execute(select(TenantQueryPattern).where(TenantQueryPattern.tenant_id == tenant.id))
        patterns = result.scalars().all()
        assert len(patterns) == 1
        assert "transactionshippingaddress" in patterns[0].tables_used
        assert "transaction" in patterns[0].tables_used
        # The whole point: the working SQL with the nKey join survives
        assert "sa.nKey = t.shippingAddress" in patterns[0].working_sql

    @pytest.mark.asyncio
    async def test_external_mcp_failed_query_not_stored(self, db: AsyncSession):
        """Failed external MCP queries should not get stored as patterns."""
        tenant = await create_test_tenant(db, name="Ext MCP Error Corp")

        tool_calls_log = [
            {
                "tool": "ext__abc__ns_runCustomSuiteQL",
                "params": {
                    "sqlQuery": "SELECT GROUP BY junk FROM transaction GROUP BY type",
                },
                "result_summary": '{"error": true, "message": "Invalid SQL syntax"}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            stored = await extract_and_store_pattern(db, tenant.id, "bad query", tool_calls_log)

        assert stored is False

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, db: AsyncSession):
        """Patterns from different tenants should not leak."""
        tenant_a = await create_test_tenant(db, name="Corp A")
        tenant_b = await create_test_tenant(db, name="Corp B")

        tool_calls_log_a = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT type, COUNT(id) as cnt FROM transaction GROUP BY type"},
                "result_summary": '{"columns": ["type", "cnt"], "rows": [["SalesOrd", "10"]], "row_count": 1}',
            }
        ]
        tool_calls_log_b = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT category, COUNT(id) as cnt FROM customer GROUP BY category"},
                "result_summary": '{"columns": ["category", "cnt"], "rows": [["Corp", "5"]], "row_count": 1}',
            }
        ]

        with patch("app.services.query_pattern_service._embed_text", new_callable=AsyncMock, return_value=None):
            await extract_and_store_pattern(db, tenant_a.id, "orders by type", tool_calls_log_a)
            await extract_and_store_pattern(db, tenant_b.id, "customers by category", tool_calls_log_b)
            await db.flush()

        result = await db.execute(select(TenantQueryPattern).where(TenantQueryPattern.tenant_id == tenant_a.id))
        patterns_a = result.scalars().all()
        assert len(patterns_a) == 1
        assert "transaction" in patterns_a[0].tables_used


# ---------------------------------------------------------------------------
# Unit tests: unified agent proven patterns injection
# ---------------------------------------------------------------------------


class TestProvenPatternsInjection:
    def test_patterns_injected_into_prompt(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        agent._proven_patterns = [
            {
                "question": "total sales today",
                "sql": "SELECT SUM(t.foreigntotal) FROM transaction t WHERE t.type = 'SalesOrd'",
            },
            {"question": "inventory for item X", "sql": "SELECT * FROM inventoryitemlocations WHERE item = 123"},
        ]

        prompt = agent.system_prompt
        assert "<proven_patterns>" in prompt
        assert "total sales today" in prompt
        assert "inventoryitemlocations" in prompt

    def test_no_patterns_no_dynamic_block(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        agent._proven_patterns = []

        prompt = agent.system_prompt
        # The dynamic block with actual patterns should NOT be present
        assert "Similar past queries that worked for this tenant" not in prompt

    def test_confidence_scoring_in_prompt(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )

        prompt = agent.system_prompt
        assert "CONFIDENCE SCORING" in prompt
        assert "<confidence>" in prompt
