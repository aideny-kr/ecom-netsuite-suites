"""Tests for export API endpoints."""

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import openpyxl
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.dependencies import get_current_user
from app.core.database import get_db
from app.main import app


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.email = "test@example.com"
    return user


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def override_deps(mock_user, mock_db):
    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def export_dir(tmp_path):
    with patch("app.api.v1.exports.EXPORT_DIR", tmp_path):
        yield tmp_path


class TestExportExcel:
    @pytest.mark.asyncio
    async def test_returns_xlsx(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/exports/excel",
                json={"columns": ["name", "amount"], "rows": [["Alice", 100], ["Bob", 200]], "title": "Test Export"},
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        assert "spreadsheetml" in response.headers["content-type"]
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        assert wb.active is not None

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app.dependency_overrides.clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/exports/excel", json={"columns": ["a"], "rows": [[1]]})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_50k_limit(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/exports/excel",
                json={"columns": ["a"], "rows": [[i] for i in range(50_001)]},
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_logged(self):
        with patch("app.api.v1.exports.audit_service") as mock_audit:
            mock_audit.log_event = AsyncMock()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post(
                    "/api/v1/exports/excel",
                    json={"columns": ["a"], "rows": [[1]]},
                    headers={"Authorization": "Bearer test"},
                )
            mock_audit.log_event.assert_called_once()


class TestQueryExport:
    @pytest.mark.asyncio
    async def test_validates_readonly(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/exports/query-export",
                json={"query_text": "DELETE FROM transaction WHERE id = 1", "format": "xlsx"},
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_no_connection(self):
        with patch("app.api.v1.exports._get_netsuite_connection", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/exports/query-export",
                    json={"query_text": "SELECT id FROM transaction", "format": "xlsx"},
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 400
            assert "No active NetSuite connection" in response.json()["detail"]


class TestDownloadExport:
    @pytest.mark.asyncio
    async def test_serves_file(self, export_dir):
        test_file = export_dir / "test_export.csv"
        test_file.write_text("a,b\n1,2\n")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/exports/test_export.csv", headers={"Authorization": "Bearer test"})
        assert response.status_code == 200
        assert "a,b" in response.text

    @pytest.mark.asyncio
    async def test_requires_auth(self, export_dir):
        app.dependency_overrides.clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/exports/test.csv")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_404_missing(self, export_dir):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/exports/nonexistent.csv", headers={"Authorization": "Bearer test"})
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_traversal(self, export_dir):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/exports/..passwd", headers={"Authorization": "Bearer test"})
        assert response.status_code == 400
