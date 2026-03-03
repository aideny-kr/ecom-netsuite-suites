"""Tests for Saved SuiteQL Queries — model, preview endpoint, and export task."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.saved_query import SavedSuiteQLQuery
from app.services.skills_service import inject_fetch_limit, paginate_suiteql, rows_to_csv
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


# ---------------------------------------------------------------------------
# Step 1: Model tests (require DB)
# ---------------------------------------------------------------------------


class TestSavedSuiteQLQueryModel:
    @pytest.mark.asyncio
    async def test_insert_and_query(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Query Corp")
        query = SavedSuiteQLQuery(
            tenant_id=tenant.id,
            name="Monthly Revenue",
            description="Revenue by month",
            query_text="SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month, SUM(t.total) as total FROM transaction t WHERE t.type = 'CustInvc' GROUP BY TO_CHAR(t.trandate, 'YYYY-MM')",
        )
        db.add(query)
        await db.flush()

        result = await db.execute(
            select(SavedSuiteQLQuery).where(SavedSuiteQLQuery.id == query.id)
        )
        fetched = result.scalar_one()
        assert fetched.name == "Monthly Revenue"
        assert fetched.tenant_id == tenant.id
        assert fetched.query_text.startswith("SELECT")
        assert fetched.created_at is not None

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, db: AsyncSession):
        """Queries from different tenants should not leak."""
        tenant_a = await create_test_tenant(db, name="Corp A")
        tenant_b = await create_test_tenant(db, name="Corp B")

        q_a = SavedSuiteQLQuery(
            tenant_id=tenant_a.id, name="A query", query_text="SELECT 1"
        )
        q_b = SavedSuiteQLQuery(
            tenant_id=tenant_b.id, name="B query", query_text="SELECT 2"
        )
        db.add_all([q_a, q_b])
        await db.flush()

        result = await db.execute(
            select(SavedSuiteQLQuery).where(
                SavedSuiteQLQuery.tenant_id == tenant_a.id
            )
        )
        queries = result.scalars().all()
        assert len(queries) == 1
        assert queries[0].name == "A query"


# ---------------------------------------------------------------------------
# Unit tests for service functions (no DB required)
# ---------------------------------------------------------------------------


class TestInjectFetchLimit:
    def test_appends_fetch_clause(self):
        sql = "SELECT id FROM transaction"
        result = inject_fetch_limit(sql, limit=500)
        assert result == "SELECT id FROM transaction FETCH FIRST 500 ROWS ONLY"

    def test_replaces_existing_fetch_clause(self):
        sql = "SELECT id FROM transaction FETCH FIRST 1000 ROWS ONLY"
        result = inject_fetch_limit(sql, limit=500)
        assert result == "SELECT id FROM transaction FETCH FIRST 500 ROWS ONLY"

    def test_strips_trailing_semicolon(self):
        sql = "SELECT id FROM transaction;"
        result = inject_fetch_limit(sql, limit=500)
        assert result == "SELECT id FROM transaction FETCH FIRST 500 ROWS ONLY"

    def test_handles_complex_query(self):
        sql = """SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month,
    SUM(t.total) as total
FROM transaction t
WHERE t.type = 'CustInvc'
GROUP BY TO_CHAR(t.trandate, 'YYYY-MM')
ORDER BY month"""
        result = inject_fetch_limit(sql, limit=500)
        assert result.endswith("FETCH FIRST 500 ROWS ONLY")
        assert "ORDER BY month" in result


class TestRowsToCsv:
    def test_basic_csv(self):
        csv_str = rows_to_csv(["id", "name"], [["1", "Alice"], ["2", "Bob"]])
        lines = csv_str.strip().splitlines()
        assert lines[0] == "id,name"
        assert lines[1] == "1,Alice"
        assert lines[2] == "2,Bob"

    def test_empty_rows(self):
        csv_str = rows_to_csv(["id"], [])
        assert csv_str.strip() == "id"


# ---------------------------------------------------------------------------
# Step 2: Preview endpoint tests (require DB)
# ---------------------------------------------------------------------------


class TestListEndpoint:
    @pytest.mark.asyncio
    async def test_list_returns_tenant_queries(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="List Corp", plan="pro")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        headers = make_auth_headers(user)

        q1 = SavedSuiteQLQuery(tenant_id=tenant.id, name="Query 1", query_text="SELECT 1")
        q2 = SavedSuiteQLQuery(tenant_id=tenant.id, name="Query 2", query_text="SELECT 2")
        db.add_all([q1, q2])
        await db.flush()

        resp = await client.get("/api/v1/skills", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {q["name"] for q in data}
        assert names == {"Query 1", "Query 2"}

    @pytest.mark.asyncio
    async def test_list_excludes_other_tenant(self, client: AsyncClient, db: AsyncSession):
        tenant_a = await create_test_tenant(db, name="Corp A", plan="pro")
        tenant_b = await create_test_tenant(db, name="Corp B", plan="pro")
        user_a, _ = await create_test_user(db, tenant_a, role_name="admin")
        headers_a = make_auth_headers(user_a)

        db.add(SavedSuiteQLQuery(tenant_id=tenant_a.id, name="A query", query_text="SELECT 1"))
        db.add(SavedSuiteQLQuery(tenant_id=tenant_b.id, name="B query", query_text="SELECT 2"))
        await db.flush()

        resp = await client.get("/api/v1/skills", headers=headers_a)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "A query"


class TestPreviewEndpoint:
    @pytest_asyncio.fixture
    async def setup(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Preview Corp", plan="pro")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        headers = make_auth_headers(user)

        query = SavedSuiteQLQuery(
            tenant_id=tenant.id,
            name="Test Query",
            query_text="SELECT id, tranid FROM transaction WHERE type = 'SalesOrd'",
        )
        db.add(query)
        await db.flush()
        return tenant, user, headers, query

    @pytest.mark.asyncio
    async def test_preview_injects_fetch_limit(self, client: AsyncClient, setup):
        tenant, user, headers, query = setup

        mock_result = {
            "columns": ["id", "tranid"],
            "rows": [["1", "SO-001"], ["2", "SO-002"]],
            "row_count": 2,
            "truncated": False,
        }

        with patch(
            "app.api.v1.skills.execute_suiteql_for_tenant", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_result
            resp = await client.post(
                "/api/v1/skills/preview",
                json={"query_id": str(query.id)},
                headers=headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["id", "tranid"]
        assert len(data["rows"]) == 2

        # Verify the FETCH FIRST 500 ROWS ONLY was injected
        called_query = mock_exec.call_args[1]["query"]
        assert "FETCH FIRST 500 ROWS ONLY" in called_query

    @pytest.mark.asyncio
    async def test_preview_404_for_missing_query(self, client: AsyncClient, setup):
        _, _, headers, _ = setup
        resp = await client.post(
            "/api/v1/skills/preview",
            json={"query_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_preview_403_cross_tenant(self, client: AsyncClient, db: AsyncSession, setup):
        """A user from tenant B cannot preview tenant A's query."""
        _, _, _, query_a = setup
        tenant_b = await create_test_tenant(db, name="Other Corp", plan="pro")
        user_b, _ = await create_test_user(db, tenant_b, role_name="admin")
        headers_b = make_auth_headers(user_b)

        resp = await client.post(
            "/api/v1/skills/preview",
            json={"query_id": str(query_a.id)},
            headers=headers_b,
        )
        assert resp.status_code == 404  # not found because tenant-scoped


# ---------------------------------------------------------------------------
# Step 3: Pagination + export tests
# ---------------------------------------------------------------------------


class TestPaginateSuiteql:
    @pytest.mark.asyncio
    async def test_aggregates_all_chunks(self):
        """The paginator should loop through pages until a page has fewer than chunk_size rows."""
        # Simulate 3 pages: 1000 + 1000 + 500 rows
        pages = [
            {
                "columns": ["id", "name"],
                "rows": [[str(i), f"item_{i}"] for i in range(1000)],
                "row_count": 1000,
                "truncated": False,
            },
            {
                "columns": ["id", "name"],
                "rows": [[str(i), f"item_{i}"] for i in range(1000, 2000)],
                "row_count": 1000,
                "truncated": False,
            },
            {
                "columns": ["id", "name"],
                "rows": [[str(i), f"item_{i}"] for i in range(2000, 2500)],
                "row_count": 500,
                "truncated": False,
            },
        ]

        call_count = 0

        async def mock_execute(*, access_token, account_id, query, limit=1000):
            nonlocal call_count
            result = pages[call_count]
            call_count += 1
            return result

        result = await paginate_suiteql(
            execute_fn=mock_execute,
            access_token="tok",
            account_id="1234",
            query="SELECT id, name FROM item",
            chunk_size=1000,
        )

        assert len(result["rows"]) == 2500
        assert result["columns"] == ["id", "name"]
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_single_page(self):
        """When results fit in one page, only one call should be made."""
        async def mock_execute(*, access_token, account_id, query, limit=1000):
            return {
                "columns": ["id"],
                "rows": [["1"], ["2"]],
                "row_count": 2,
                "truncated": False,
            }

        result = await paginate_suiteql(
            execute_fn=mock_execute,
            access_token="tok",
            account_id="1234",
            query="SELECT id FROM item",
            chunk_size=1000,
        )

        assert len(result["rows"]) == 2
        assert result["row_count"] == 2


class TestExportTriggerEndpoint:
    @pytest.mark.asyncio
    async def test_returns_202_with_task_id(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Export Corp", plan="pro")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        headers = make_auth_headers(user)

        query = SavedSuiteQLQuery(
            tenant_id=tenant.id,
            name="Export Query",
            query_text="SELECT id FROM transaction",
        )
        db.add(query)
        await db.flush()

        with patch(
            "app.workers.tasks.suiteql_export.export_suiteql_to_csv"
        ) as mock_task:
            mock_task.delay.return_value = MagicMock(id="celery-task-123")
            resp = await client.post(
                "/api/v1/skills/export",
                json={"query_id": str(query.id)},
                headers=headers,
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["task_id"] == "celery-task-123"
        assert data["status"] == "queued"
