"""Tests for job trigger and schedules endpoints."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.main import app


@pytest.fixture
def mock_admin():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    ur = MagicMock()
    ur.role_id = uuid.uuid4()
    ur.role = MagicMock()
    ur.role.name = "admin"
    user.user_roles = [ur]
    return user


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    # Make execute return permission codenames for require_permission dependency
    perm_result = MagicMock()
    perm_result.all.return_value = [
        ("tenant.manage",),
        ("tables.view",),
    ]
    perm_scalars = MagicMock()
    perm_scalars.all.return_value = []
    exec_result = MagicMock()
    exec_result.all.return_value = perm_result.all.return_value
    exec_result.scalars.return_value = perm_scalars
    exec_result.scalar.return_value = 0
    db.execute = AsyncMock(return_value=exec_result)
    return db


@pytest.fixture(autouse=True)
def override_deps(mock_admin, mock_db):
    app.dependency_overrides[get_current_user] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()


def _make_job(tenant_id, status="completed", job_type="knowledge_crawler"):
    """Create a mock Job-like object."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.tenant_id = tenant_id
    job.job_type = job_type
    job.status = status
    job.correlation_id = None
    job.connection_id = None
    job.started_at = None
    job.completed_at = None
    job.parameters = None
    job.result_summary = None
    job.error_message = None
    job.celery_task_id = None
    job.created_at = MagicMock()
    return job


class TestJobsListFiltering:
    """Tests for GET /jobs — status filter and pagination."""

    @pytest.mark.asyncio
    async def test_status_filter_param_accepted(self, mock_admin, mock_db):
        """GET /jobs?status=completed should only return completed jobs."""
        jobs = [_make_job(mock_admin.tenant_id, "completed")]

        # First call: permission check, second: count, third: query
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        query_result = MagicMock()
        query_result.scalars.return_value.all.return_value = jobs

        perm_result = MagicMock()
        perm_result.all.return_value = [("tables.view",)]

        mock_db.execute = AsyncMock(side_effect=[perm_result, count_result, query_result])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/jobs?status=completed",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert all(item["status"] == "completed" for item in data["items"])

    @pytest.mark.asyncio
    async def test_status_filter_excludes_other_statuses(self, mock_admin, mock_db):
        """When status=completed, failed jobs should not appear."""
        # Return 0 jobs for a status that doesn't exist
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        query_result = MagicMock()
        query_result.scalars.return_value.all.return_value = []

        perm_result = MagicMock()
        perm_result.all.return_value = [("tables.view",)]

        mock_db.execute = AsyncMock(side_effect=[perm_result, count_result, query_result])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/jobs?status=completed",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, mock_admin, mock_db):
        """Without status param, all jobs are returned."""
        jobs = [
            _make_job(mock_admin.tenant_id, "completed"),
            _make_job(mock_admin.tenant_id, "failed"),
        ]

        count_result = MagicMock()
        count_result.scalar.return_value = 2
        query_result = MagicMock()
        query_result.scalars.return_value.all.return_value = jobs

        perm_result = MagicMock()
        perm_result.all.return_value = [("tables.view",)]

        mock_db.execute = AsyncMock(side_effect=[perm_result, count_result, query_result])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/jobs",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_pagination_params(self, mock_admin, mock_db):
        """page and page_size params work correctly."""
        jobs = [_make_job(mock_admin.tenant_id, "completed")]

        count_result = MagicMock()
        count_result.scalar.return_value = 15
        query_result = MagicMock()
        query_result.scalars.return_value.all.return_value = jobs

        perm_result = MagicMock()
        perm_result.all.return_value = [("tables.view",)]

        mock_db.execute = AsyncMock(side_effect=[perm_result, count_result, query_result])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/jobs?page=2&page_size=5",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 15
        assert data["page"] == 2
        assert data["page_size"] == 5
        assert data["pages"] == 3


class TestTriggerJob:
    @pytest.mark.asyncio
    async def test_trigger_knowledge_crawler(self):
        with (
            patch("app.workers.celery_app.celery_app") as mock_celery,
            patch("app.services.audit_service.log_event", new_callable=AsyncMock),
        ):
            mock_result = MagicMock()
            mock_result.id = "test-task-id"
            mock_celery.send_task.return_value = mock_result

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/jobs/trigger/knowledge_crawler",
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 202
            assert response.json()["status"] == "queued"

    @pytest.mark.asyncio
    async def test_trigger_auto_learning(self):
        with (
            patch("app.workers.celery_app.celery_app") as mock_celery,
            patch("app.services.audit_service.log_event", new_callable=AsyncMock),
        ):
            mock_result = MagicMock()
            mock_result.id = "test-task-id-2"
            mock_celery.send_task.return_value = mock_result

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/jobs/trigger/auto_learning",
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 202
            assert response.json()["task_name"] == "auto_learning"

    @pytest.mark.asyncio
    async def test_trigger_invalid_task(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/jobs/trigger/nonexistent_task",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app.dependency_overrides.clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/jobs/trigger/knowledge_crawler")
        assert response.status_code in (401, 403)


class TestListSchedules:
    @pytest.mark.asyncio
    async def test_returns_schedules(self):
        with patch("app.workers.celery_app.celery_app") as mock_celery:
            mock_celery.conf.beat_schedule = {
                "knowledge-crawler": {
                    "task": "tasks.knowledge_crawler",
                    "schedule": "crontab(hour=3, minute=0)",
                },
                "auto-learning": {
                    "task": "tasks.auto_learning",
                    "schedule": "crontab(hour=4, minute=0)",
                },
            }

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(
                    "/api/v1/jobs/schedules",
                    headers={"Authorization": "Bearer test"},
                )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        task_names = [s["task"] for s in data]
        assert "tasks.knowledge_crawler" in task_names
        assert "tasks.auto_learning" in task_names
