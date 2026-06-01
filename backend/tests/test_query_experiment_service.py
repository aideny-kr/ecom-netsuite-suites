"""Tests for query experiment service.

Decision-threshold tests live in test_experiment_benchmark_scoring.py —
this file covers SQL generation/execution failure paths, cost estimation,
and pattern-promotion side effects.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.query_eval_harness import EvalCase

_SVC = "app.services.query_experiment_service"


class TestRunExperiment:
    @pytest.mark.asyncio
    async def test_execution_error_returns_skip(self):
        from app.services.query_experiment_service import run_single_experiment

        case = EvalCase(
            question="Bad query",
            dialect="suiteql",
            expected_keywords=["test"],
            expected_sql_contains=[],
            tables=["transaction"],
            difficulty="easy",
        )

        with (
            patch(f"{_SVC}._generate_sql", new_callable=AsyncMock) as mock_gen,
            patch(f"{_SVC}._execute_sql", new_callable=AsyncMock) as mock_exec,
        ):
            mock_gen.return_value = "INVALID SQL QUERY"
            mock_exec.return_value = {
                "success": False,
                "error": "Syntax error near INVALID",
                "result_text": "",
                "rows": 0,
                "bytes_processed": 0,
            }

            result = await run_single_experiment(
                case=case,
                tenant_id=uuid.uuid4(),
                db=AsyncMock(),
            )

        assert result["decision"] == "SKIP"
        assert result["error_message"]

    @pytest.mark.asyncio
    async def test_generation_failure_returns_skip(self):
        from app.services.query_experiment_service import run_single_experiment

        case = EvalCase(
            question="Complex query",
            dialect="bigquery",
            expected_keywords=["revenue"],
            expected_sql_contains=[],
            tables=["sales"],
            difficulty="hard",
        )

        with patch("app.services.query_experiment_service._generate_sql", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = None  # Generation failed

            result = await run_single_experiment(
                case=case,
                tenant_id=uuid.uuid4(),
                db=AsyncMock(),
            )

        assert result["decision"] == "SKIP"

class TestBudgetEstimation:
    def test_suiteql_cost(self):
        from app.services.query_experiment_service import estimate_experiment_cost

        cost = estimate_experiment_cost("suiteql")
        assert 0.01 < cost < 0.50

    def test_bigquery_cost(self):
        from app.services.query_experiment_service import estimate_experiment_cost

        cost = estimate_experiment_cost("bigquery")
        assert 0.01 < cost < 0.50

    def test_bigquery_more_expensive(self):
        from app.services.query_experiment_service import estimate_experiment_cost

        assert estimate_experiment_cost("bigquery") >= estimate_experiment_cost("suiteql")


class TestGenerateSQL:
    @pytest.mark.asyncio
    async def test_generate_returns_sql_string(self):
        from app.services.query_experiment_service import _generate_sql

        with patch("app.services.query_experiment_service._call_haiku", new_callable=AsyncMock) as mock_haiku:
            mock_haiku.return_value = "SELECT id FROM transaction FETCH FIRST 10 ROWS ONLY"

            sql = await _generate_sql(
                question="Show me 10 transactions",
                dialect="suiteql",
                schema_hint="Tables: transaction (id, tranid, type, status, total)",
            )

        assert sql is not None
        assert "SELECT" in sql

    @pytest.mark.asyncio
    async def test_generate_returns_none_on_error(self):
        from app.services.query_experiment_service import _generate_sql

        with patch("app.services.query_experiment_service._call_haiku", new_callable=AsyncMock) as mock_haiku:
            mock_haiku.side_effect = Exception("API error")

            sql = await _generate_sql(
                question="test",
                dialect="suiteql",
                schema_hint="",
            )

        assert sql is None

    @pytest.mark.asyncio
    async def test_generate_strips_markdown_fences(self):
        from app.services.query_experiment_service import _generate_sql

        with patch("app.services.query_experiment_service._call_haiku", new_callable=AsyncMock) as mock_haiku:
            mock_haiku.return_value = "```sql\nSELECT 1\n```"

            sql = await _generate_sql(
                question="test",
                dialect="suiteql",
            )

        assert sql == "SELECT 1"
        assert "```" not in sql


class TestPromoteExperimentResult:
    @pytest.mark.asyncio
    async def test_keep_stores_pattern(self):
        from app.services.query_experiment_service import promote_experiment_result

        mock_db = AsyncMock()
        result = {
            "decision": "KEEP",
            "test_query": "Top 10 sales orders",
            "generated_sql": "SELECT tranid FROM transaction ORDER BY total DESC FETCH FIRST 10 ROWS ONLY",
            "dialect": "suiteql",
            "experiment_score": 0.85,
            "baseline_score": 0.70,
            "cost_usd": 0.15,
        }

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            mock_store.return_value = True
            await promote_experiment_result(result, uuid.uuid4(), mock_db)
            mock_store.assert_called_once()

    @pytest.mark.asyncio
    async def test_revert_does_not_store_pattern(self):
        from app.services.query_experiment_service import promote_experiment_result

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            result = {"decision": "REVERT", "test_query": "test", "dialect": "suiteql"}
            await promote_experiment_result(result, uuid.uuid4(), AsyncMock())
            mock_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_does_not_store_pattern(self):
        from app.services.query_experiment_service import promote_experiment_result

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            result = {"decision": "SKIP", "test_query": "test", "dialect": "suiteql"}
            await promote_experiment_result(result, uuid.uuid4(), AsyncMock())
            mock_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_experiment_to_db(self):
        from app.services.query_experiment_service import promote_experiment_result

        mock_db = AsyncMock()
        result = {
            "decision": "KEEP",
            "test_query": "Revenue by region",
            "generated_sql": "SELECT region, SUM(total) FROM transaction GROUP BY region",
            "dialect": "bigquery",
            "experiment_score": 0.90,
            "baseline_score": 0.75,
            "delta": 0.15,
            "cost_usd": 0.20,
            "hypothesis": "Test BigQuery query generation",
        }

        with patch("app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock):
            await promote_experiment_result(result, uuid.uuid4(), mock_db)
            # Should have added an ExperimentLog to the session
            mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_experiment_on_revert(self):
        from app.services.query_experiment_service import promote_experiment_result

        mock_db = AsyncMock()
        result = {
            "decision": "REVERT",
            "test_query": "Find slow queries",
            "generated_sql": "SELECT id FROM transaction FETCH FIRST 5 ROWS ONLY",
            "dialect": "suiteql",
            "experiment_score": 0.40,
            "baseline_score": 0.80,
            "delta": -0.40,
            "cost_usd": 0.15,
        }

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            await promote_experiment_result(result, uuid.uuid4(), mock_db)
            # Should log to DB even on REVERT
            mock_db.add.assert_called_once()
            # But should NOT promote to patterns
            mock_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_keep_uses_correct_tool_name_for_suiteql(self):
        from app.services.query_experiment_service import promote_experiment_result

        mock_db = AsyncMock()
        result = {
            "decision": "KEEP",
            "test_query": "Top customers by revenue",
            "generated_sql": "SELECT entity, SUM(total) FROM transaction GROUP BY entity FETCH FIRST 20 ROWS ONLY",
            "dialect": "suiteql",
            "experiment_score": 0.90,
            "baseline_score": 0.70,
        }

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            mock_store.return_value = True
            await promote_experiment_result(result, uuid.uuid4(), mock_db)
            call_args = mock_store.call_args
            tool_calls_log = call_args[0][3]  # 4th positional arg
            assert tool_calls_log[0]["tool"] == "netsuite_suiteql"

    @pytest.mark.asyncio
    async def test_keep_uses_correct_tool_name_for_bigquery(self):
        from app.services.query_experiment_service import promote_experiment_result

        mock_db = AsyncMock()
        result = {
            "decision": "KEEP",
            "test_query": "Sales by product category",
            "generated_sql": "SELECT category, SUM(revenue) FROM `proj.ds.sales` GROUP BY category LIMIT 20",
            "dialect": "bigquery",
            "experiment_score": 0.88,
            "baseline_score": 0.70,
        }

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            mock_store.return_value = True
            await promote_experiment_result(result, uuid.uuid4(), mock_db)
            call_args = mock_store.call_args
            tool_calls_log = call_args[0][3]
            assert tool_calls_log[0]["tool"] == "bigquery_sql"

    @pytest.mark.asyncio
    async def test_keep_with_empty_sql_does_not_store(self):
        from app.services.query_experiment_service import promote_experiment_result

        mock_db = AsyncMock()
        result = {
            "decision": "KEEP",
            "test_query": "Some question",
            "generated_sql": "",  # Empty SQL
            "dialect": "suiteql",
        }

        with patch(
            "app.services.query_experiment_service.extract_and_store_pattern", new_callable=AsyncMock
        ) as mock_store:
            await promote_experiment_result(result, uuid.uuid4(), mock_db)
            # Still logs to DB but does not promote
            mock_db.add.assert_called_once()
            mock_store.assert_not_called()


class TestExecuteSQL:
    @pytest.mark.asyncio
    async def test_unknown_dialect_returns_failure(self):
        from app.services.query_experiment_service import _execute_sql

        result = await _execute_sql(
            sql="SELECT 1",
            dialect="mysql",
            tenant_id=uuid.uuid4(),
            db=AsyncMock(),
        )

        assert result["success"] is False
        assert "Unknown dialect" in result["error"]
