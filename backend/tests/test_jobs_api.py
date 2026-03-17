"""Tests for job trigger and schedules endpoints."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.dependencies import get_current_user
from app.core.database import get_db
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


class TestTriggerJob:
    @pytest.mark.asyncio
    async def test_trigger_knowledge_crawler(self):
        with patch("app.workers.celery_app.celery_app") as mock_celery, \
             patch("app.services.audit_service.log_event", new_callable=AsyncMock):
            mock_result = MagicMock()
            mock_result.id = "test-task-id"
            mock_celery.send_task.return_value = mock_result

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/jobs/trigger/knowledge_crawler",
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 202
            assert response.json()["status"] == "queued"

    @pytest.mark.asyncio
    async def test_trigger_auto_learning(self):
        with patch("app.workers.celery_app.celery_app") as mock_celery, \
             patch("app.services.audit_service.log_event", new_callable=AsyncMock):
            mock_result = MagicMock()
            mock_result.id = "test-task-id-2"
            mock_celery.send_task.return_value = mock_result

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/jobs/trigger/auto_learning",
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 202
            assert response.json()["task_name"] == "auto_learning"

    @pytest.mark.asyncio
    async def test_trigger_invalid_task(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/jobs/trigger/nonexistent_task",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app.dependency_overrides.clear()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
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

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
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
