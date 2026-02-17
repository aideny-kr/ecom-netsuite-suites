"""Tests for BYOK AI settings API — CRUD, encryption, validation, RBAC, tenant isolation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestAiSchemaValidation:
    """Test Pydantic validation on AI config schemas."""

    def test_valid_provider(self):
        from app.schemas.tenant import TenantConfigUpdate
        update = TenantConfigUpdate(ai_provider="openai")
        assert update.ai_provider == "openai"

    def test_invalid_provider_rejected(self):
        from app.schemas.tenant import TenantConfigUpdate
        with pytest.raises(ValueError, match="Invalid provider"):
            TenantConfigUpdate(ai_provider="mistral")

    def test_valid_model(self):
        from app.schemas.tenant import TenantConfigUpdate
        update = TenantConfigUpdate(ai_model="gpt-5.2")
        assert update.ai_model == "gpt-5.2"

    def test_invalid_model_rejected(self):
        from app.schemas.tenant import TenantConfigUpdate
        with pytest.raises(ValueError, match="Invalid model"):
            TenantConfigUpdate(ai_model="gpt-999")

    def test_none_provider_allowed(self):
        from app.schemas.tenant import TenantConfigUpdate
        update = TenantConfigUpdate(ai_provider=None)
        assert update.ai_provider is None

    def test_ai_key_test_request_validates_provider(self):
        from app.schemas.tenant import AiKeyTestRequest
        with pytest.raises(ValueError, match="Invalid provider"):
            AiKeyTestRequest(provider="bad", api_key="key")


# ---------------------------------------------------------------------------
# API integration tests (require db)
# ---------------------------------------------------------------------------


class TestAiSettingsApi:
    @pytest.mark.asyncio
    async def test_get_config_includes_ai_fields(self, client, db, tenant_a, admin_user):
        user, headers = admin_user
        resp = await client.get("/api/v1/tenants/me/config", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "ai_provider" in data
        assert "ai_model" in data
        assert "ai_api_key_set" in data
        assert data["ai_api_key_set"] is False

    @pytest.mark.asyncio
    async def test_patch_config_encrypts_key(self, client, db, tenant_a, admin_user):
        user, headers = admin_user
        resp = await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_provider": "openai", "ai_api_key": "sk-test-key-123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_provider"] == "openai"
        assert data["ai_api_key_set"] is True
        # Raw key is never returned
        assert "sk-test-key-123" not in str(data)
        assert "ai_api_key" not in data or data.get("ai_api_key") is None
        assert "ai_api_key_encrypted" not in data

    @pytest.mark.asyncio
    async def test_get_config_never_returns_raw_key(self, client, db, tenant_a, admin_user):
        user, headers = admin_user
        # Set a key first
        await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_provider": "anthropic", "ai_api_key": "secret-key"},
        )
        # Get config
        resp = await client.get("/api/v1/tenants/me/config", headers=headers)
        data = resp.json()
        assert "secret-key" not in str(data)
        assert data["ai_api_key_set"] is True

    @pytest.mark.asyncio
    async def test_clear_provider_clears_key(self, client, db, tenant_a, admin_user):
        user, headers = admin_user
        # Set provider + key
        await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_provider": "openai", "ai_api_key": "sk-key"},
        )
        # Clear provider
        resp = await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_provider": None},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_provider"] is None
        assert data["ai_model"] is None
        assert data["ai_api_key_set"] is False

    @pytest.mark.asyncio
    async def test_invalid_model_for_provider(self, client, db, tenant_a, admin_user):
        user, headers = admin_user
        # Set provider to openai, then try to set an anthropic model
        await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_provider": "openai"},
        )
        resp = await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_model": "claude-sonnet-4-20250514"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_test_ai_key_endpoint(self, client, db, tenant_a, admin_user):
        user, headers = admin_user

        # Mock the adapter to simulate a successful test
        with patch("app.api.v1.tenants.get_adapter") as mock_get:
            mock_adapter = MagicMock()
            mock_usage = MagicMock(input_tokens=1, output_tokens=1)
            mock_adapter.create_message = AsyncMock(
                return_value=MagicMock(
                    text_blocks=["hi"], tool_use_blocks=[], usage=mock_usage,
                )
            )
            mock_get.return_value = mock_adapter

            resp = await client.post(
                "/api/v1/tenants/me/config/test-ai-key",
                headers=headers,
                json={"provider": "openai", "api_key": "sk-test"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True

    @pytest.mark.asyncio
    async def test_test_ai_key_failure(self, client, db, tenant_a, admin_user):
        user, headers = admin_user

        with patch("app.api.v1.tenants.get_adapter") as mock_get:
            mock_adapter = MagicMock()
            mock_adapter.create_message = AsyncMock(side_effect=Exception("Invalid API key"))
            mock_get.return_value = mock_adapter

            resp = await client.post(
                "/api/v1/tenants/me/config/test-ai-key",
                headers=headers,
                json={"provider": "openai", "api_key": "bad-key"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "Invalid API key" in data["error"]


class TestAiSettingsRbac:
    """RBAC negative tests — non-admin users should be blocked."""

    @pytest.mark.asyncio
    async def test_readonly_cannot_update_ai_config(self, client, db, tenant_a, readonly_user):
        _, headers = readonly_user
        resp = await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers,
            json={"ai_provider": "openai"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_readonly_cannot_test_ai_key(self, client, db, tenant_a, readonly_user):
        _, headers = readonly_user
        resp = await client.post(
            "/api/v1/tenants/me/config/test-ai-key",
            headers=headers,
            json={"provider": "openai", "api_key": "sk-test"},
        )
        assert resp.status_code == 403


class TestAiSettingsTenantIsolation:
    """Tenant isolation — one tenant can't see/modify another's AI config."""

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_see_tenant_a_key(self, client, db, tenant_a, tenant_b, admin_user, admin_user_b):
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        # Tenant A sets a key
        await client.patch(
            "/api/v1/tenants/me/config",
            headers=headers_a,
            json={"ai_provider": "openai", "ai_api_key": "tenant-a-secret"},
        )

        # Tenant B should not see it
        resp = await client.get("/api/v1/tenants/me/config", headers=headers_b)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_api_key_set"] is False
        assert "tenant-a-secret" not in str(data)
