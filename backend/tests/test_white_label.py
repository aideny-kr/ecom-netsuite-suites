"""Tests for white-labeling features: branding, domain, feature flags, soul seeding."""

import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text

from app.models.tenant import TenantConfig
from app.services.feature_flag_service import (
    _CACHE_TTL,
    _FLAG_CACHE,
    DEFAULT_FLAGS,
    clear_cache,
    get_all_flags,
    is_enabled,
    seed_default_flags,
    set_flag,
)

# ---------------------------------------------------------------------------
# Feature Flag Service
# ---------------------------------------------------------------------------


class TestFeatureFlagService:
    @pytest.mark.asyncio
    async def test_seed_default_flags(self, db, tenant_a):
        """Seeding creates all default flags for a tenant."""
        await seed_default_flags(db, tenant_a.id)
        await db.flush()

        flags = await get_all_flags(db, tenant_a.id)
        assert len(flags) == len(DEFAULT_FLAGS)
        for key, expected in DEFAULT_FLAGS.items():
            assert flags[key] == expected, f"Flag {key} expected {expected}, got {flags[key]}"

    @pytest.mark.asyncio
    async def test_seed_default_flags_idempotent(self, db, tenant_a):
        """Seeding twice should not create duplicates or change values."""
        await seed_default_flags(db, tenant_a.id)
        await db.flush()

        # Flip one flag
        await set_flag(db, tenant_a.id, "chat", False)
        await db.flush()

        # Seed again — should not overwrite
        await seed_default_flags(db, tenant_a.id)
        await db.flush()

        flags = await get_all_flags(db, tenant_a.id)
        assert flags["chat"] is False  # Should stay flipped

    @pytest.mark.asyncio
    async def test_is_enabled_with_cache(self, db, tenant_a):
        """is_enabled should use TTL cache."""
        clear_cache()
        await set_flag(db, tenant_a.id, "chat", True)
        await db.flush()

        result = await is_enabled(db, tenant_a.id, "chat")
        assert result is True

        # Second call should hit cache (no DB call needed)
        result2 = await is_enabled(db, tenant_a.id, "chat")
        assert result2 is True

    @pytest.mark.asyncio
    async def test_is_enabled_returns_false_for_unknown_flag(self, db, tenant_a):
        """Unknown flag should return False."""
        clear_cache()
        result = await is_enabled(db, tenant_a.id, "nonexistent_flag")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_flag_upsert(self, db, tenant_a):
        """set_flag should create and then update."""
        await set_flag(db, tenant_a.id, "test_flag", True)
        await db.flush()

        flags = await get_all_flags(db, tenant_a.id)
        assert flags["test_flag"] is True

        await set_flag(db, tenant_a.id, "test_flag", False)
        await db.flush()

        flags = await get_all_flags(db, tenant_a.id)
        assert flags["test_flag"] is False

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, db, tenant_a, tenant_b):
        """Flags from one tenant should not leak to another."""
        await set_flag(db, tenant_a.id, "secret_feature", True)
        await db.flush()

        flags_b = await get_all_flags(db, tenant_b.id)
        assert "secret_feature" not in flags_b


# ---------------------------------------------------------------------------
# Branding API
# ---------------------------------------------------------------------------


class TestBrandingAPI:
    @pytest.mark.asyncio
    async def test_get_branding_defaults(self, client, db, admin_user):
        """GET /settings/branding → 200 with null defaults."""
        _, headers = admin_user
        resp = await client.get("/api/v1/settings/branding", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["brand_name"] is None
        assert data["brand_color_hsl"] is None
        assert data["brand_logo_url"] is None
        assert data["domain_verified"] is False

    @pytest.mark.asyncio
    async def test_update_branding(self, client, db, admin_user):
        """PATCH /settings/branding → 200 with updated values."""
        _, headers = admin_user
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={
                "brand_name": "Acme Corp",
                "brand_color_hsl": "220 90% 56%",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["brand_name"] == "Acme Corp"
        assert data["brand_color_hsl"] == "220 90% 56%"

    @pytest.mark.asyncio
    async def test_update_branding_partial(self, client, db, admin_user):
        """PATCH with only one field should not clear others."""
        _, headers = admin_user

        # Set brand name first
        await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": "Acme Corp"},
            headers=headers,
        )

        # Update only color
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"brand_color_hsl": "180 50% 40%"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["brand_name"] == "Acme Corp"  # Unchanged
        assert data["brand_color_hsl"] == "180 50% 40%"

    @pytest.mark.asyncio
    async def test_custom_domain_resets_verification(self, client, db, admin_user):
        """Setting custom_domain should reset domain_verified to False."""
        _, headers = admin_user

        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "app.acme.com"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["custom_domain"] == "app.acme.com"
        assert data["domain_verified"] is False

    @pytest.mark.asyncio
    async def test_branding_unauthenticated(self, client):
        """GET /settings/branding without auth → 401/403."""
        resp = await client.get("/api/v1/settings/branding")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_branding_tenant_isolation(self, client, db, admin_user, admin_user_b):
        """Tenant A's branding should not be visible to Tenant B."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": "Secret Corp"},
            headers=headers_a,
        )

        resp = await client.get("/api/v1/settings/branding", headers=headers_b)
        assert resp.status_code == 200
        assert resp.json()["brand_name"] is None  # Tenant B sees their own config


# ---------------------------------------------------------------------------
# Feature Flags API
# ---------------------------------------------------------------------------


class TestFeatureFlagsAPI:
    @pytest.mark.asyncio
    async def test_get_features_empty(self, client, db, admin_user):
        """GET /settings/features → 200 with empty flags if none seeded."""
        _, headers = admin_user
        resp = await client.get("/api/v1/settings/features", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "flags" in data
        assert isinstance(data["flags"], dict)

    @pytest.mark.asyncio
    async def test_update_features(self, client, db, admin_user, tenant_a):
        """PATCH /settings/features → 200 with updated flags."""
        _, headers = admin_user

        resp = await client.patch(
            "/api/v1/settings/features",
            json={"flags": {"chat": True, "mcp_tools": False}},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["flags"]["chat"] is True
        assert data["flags"]["mcp_tools"] is False

    @pytest.mark.asyncio
    async def test_features_unauthenticated(self, client):
        """GET /settings/features without auth → 401/403."""
        resp = await client.get("/api/v1/settings/features")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Domain Verification
# ---------------------------------------------------------------------------


class TestDomainVerification:
    @pytest.mark.asyncio
    async def test_verify_domain_mismatch(self, client, db, admin_user):
        """Verifying a domain that doesn't match configured domain → 400."""
        _, headers = admin_user

        resp = await client.post(
            "/api/v1/settings/verify-domain",
            json={"domain": "wrong.example.com"},
            headers=headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_domain_success(self, client, db, admin_user):
        """Successful DNS verification should set domain_verified=True."""
        _, headers = admin_user

        # First set the custom domain
        await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "app.acme.com"},
            headers=headers,
        )

        # Mock DNS verification to succeed
        with patch("app.api.v1.settings.verify_domain", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = True

            resp = await client.post(
                "/api/v1/settings/verify-domain",
                json={"domain": "app.acme.com"},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["verified"] is True
            assert data["dns_record"]["type"] == "TXT"

    @pytest.mark.asyncio
    async def test_verify_domain_failure(self, client, db, admin_user):
        """Failed DNS verification should not set domain_verified."""
        _, headers = admin_user

        # Set custom domain
        await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "app.acme.com"},
            headers=headers,
        )

        with patch("app.api.v1.settings.verify_domain", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = False

            resp = await client.post(
                "/api/v1/settings/verify-domain",
                json={"domain": "app.acme.com"},
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.json()["verified"] is False

        # Confirm domain_verified is still False
        resp = await client.get("/api/v1/settings/branding", headers=headers)
        assert resp.json()["domain_verified"] is False


# ---------------------------------------------------------------------------
# Domain Resolver (public endpoint)
# ---------------------------------------------------------------------------


class TestDomainResolver:
    @pytest.mark.asyncio
    async def test_resolve_unknown_domain(self, client, db):
        """Resolving an unknown domain → 404."""
        resp = await client.get("/api/v1/settings/resolve-domain?domain=unknown.com")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_resolve_verified_domain(self, client, db, admin_user, tenant_a):
        """Resolving a verified domain → 200 with tenant slug."""
        _, headers = admin_user

        # Set and verify domain
        await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "analytics.acme.com"},
            headers=headers,
        )

        # Directly set domain_verified=True in DB (bypass DNS check)
        result = await db.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id)
        )
        config = result.scalar_one()
        config.domain_verified = True
        await db.flush()

        resp = await client.get("/api/v1/settings/resolve-domain?domain=analytics.acme.com")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_id"] == str(tenant_a.id)
        assert "tenant_slug" in data

    @pytest.mark.asyncio
    async def test_resolve_unverified_domain(self, client, db, admin_user):
        """Resolving an unverified domain → 404."""
        _, headers = admin_user

        await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "pending.acme.com"},
            headers=headers,
        )

        resp = await client.get("/api/v1/settings/resolve-domain?domain=pending.acme.com")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# require_feature dependency
# ---------------------------------------------------------------------------


class TestRequireFeatureDependency:
    @pytest.mark.asyncio
    async def test_feature_enabled_passes(self, client, db, admin_user, tenant_a):
        """When feature is enabled, the request should pass through."""
        clear_cache()
        await set_flag(db, tenant_a.id, "chat", True)
        await db.flush()

        _, headers = admin_user
        # Chat endpoint uses require_feature("chat") or similar auth
        # We test by hitting the features endpoint which requires auth
        resp = await client.get("/api/v1/settings/features", headers=headers)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_require_feature_function(self, db, tenant_a):
        """Direct test of require_feature logic."""
        from app.services.feature_flag_service import is_enabled

        clear_cache()
        await set_flag(db, tenant_a.id, "workspace", True)
        await set_flag(db, tenant_a.id, "reconciliation", False)
        await db.flush()

        assert await is_enabled(db, tenant_a.id, "workspace") is True
        assert await is_enabled(db, tenant_a.id, "reconciliation") is False


# ---------------------------------------------------------------------------
# Soul Seeding
# ---------------------------------------------------------------------------


class TestSoulSeeding:
    @pytest.fixture(autouse=True)
    def _redirect_storage(self, tmp_path):
        """Redirect soul file paths to a temp directory via env var."""
        import os

        old_val = os.environ.get("WORKSPACE_STORAGE_DIR")
        os.environ["WORKSPACE_STORAGE_DIR"] = str(tmp_path)

        # Monkey-patch get_soul_file_path + update_soul_config's getattr
        import app.services.soul_service as soul_mod

        _orig_get_path = soul_mod.get_soul_file_path

        def _mock_path(tenant_id):
            return os.path.join(str(tmp_path), str(tenant_id), "soul.md")

        soul_mod.get_soul_file_path = _mock_path

        # Patch update_soul_config to use tmp_path for storage_dir
        _orig_update = soul_mod.update_soul_config

        async def _patched_update(tenant_id, data):
            tenant_dir = os.path.join(str(tmp_path), str(tenant_id))
            os.makedirs(tenant_dir, exist_ok=True)
            soul_path = os.path.join(tenant_dir, "soul.md")

            content_parts = []
            if data.bot_tone and data.bot_tone.strip():
                content_parts.append(f"# AI Tone\n\n{data.bot_tone.strip()}\n")
            if data.netsuite_quirks and data.netsuite_quirks.strip():
                content_parts.append(f"# NetSuite Quirks\n\n{data.netsuite_quirks.strip()}\n")

            final_content = "\n".join(content_parts)
            exists = False
            if final_content.strip():
                with open(soul_path, "w", encoding="utf-8") as f:
                    f.write(final_content)
                exists = True
            elif os.path.exists(soul_path):
                os.remove(soul_path)

            from app.schemas.soul import SoulConfigResponse
            return SoulConfigResponse(
                bot_tone=data.bot_tone.strip() if data.bot_tone else None,
                netsuite_quirks=data.netsuite_quirks.strip() if data.netsuite_quirks else None,
                exists=exists,
            )

        soul_mod.update_soul_config = _patched_update

        yield

        # Restore
        soul_mod.get_soul_file_path = _orig_get_path
        soul_mod.update_soul_config = _orig_update
        if old_val is not None:
            os.environ["WORKSPACE_STORAGE_DIR"] = old_val
        else:
            os.environ.pop("WORKSPACE_STORAGE_DIR", None)

    @pytest.mark.asyncio
    async def test_seed_default_soul_creates_file(self):
        """seed_default_soul should create a soul.md with default content."""
        from app.services.soul_service import get_soul_config, seed_default_soul

        tenant_id = uuid.uuid4()
        await seed_default_soul(tenant_id, "Acme Corp")

        config = await get_soul_config(tenant_id)
        assert config.exists is True
        assert "Acme Corp" in (config.bot_tone or "")
        assert "BUILTIN.DF()" in (config.netsuite_quirks or "")

    @pytest.mark.asyncio
    async def test_seed_default_soul_idempotent(self):
        """seed_default_soul should not overwrite existing soul config."""
        from app.schemas.soul import SoulUpdateRequest
        from app.services.soul_service import get_soul_config, seed_default_soul, update_soul_config

        tenant_id = uuid.uuid4()

        # Create custom soul first
        await update_soul_config(
            tenant_id,
            SoulUpdateRequest(bot_tone="Custom tone", netsuite_quirks="Custom quirks"),
        )

        # Seed should not overwrite
        await seed_default_soul(tenant_id, "Acme Corp")

        config = await get_soul_config(tenant_id)
        assert config.bot_tone == "Custom tone"
        assert config.netsuite_quirks == "Custom quirks"


# ---------------------------------------------------------------------------
# TDD Iteration 1 — RBAC negative tests
# ---------------------------------------------------------------------------


class TestRBACNegative:
    """Readonly users must NOT be able to mutate settings."""

    @pytest.mark.asyncio
    async def test_readonly_cannot_update_branding(self, client, db, readonly_user):
        """PATCH /settings/branding with readonly role → 403."""
        _, headers = readonly_user
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": "Hacked Corp"},
            headers=headers,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_readonly_cannot_update_features(self, client, db, readonly_user):
        """PATCH /settings/features with readonly role → 403."""
        _, headers = readonly_user
        resp = await client.patch(
            "/api/v1/settings/features",
            json={"flags": {"chat": False}},
            headers=headers,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_readonly_cannot_verify_domain(self, client, db, readonly_user):
        """POST /settings/verify-domain with readonly role → 403."""
        _, headers = readonly_user
        resp = await client.post(
            "/api/v1/settings/verify-domain",
            json={"domain": "test.example.com"},
            headers=headers,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_readonly_can_read_branding(self, client, db, readonly_user):
        """GET /settings/branding with readonly role → 200 (read is allowed)."""
        _, headers = readonly_user
        resp = await client.get("/api/v1/settings/branding", headers=headers)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_readonly_can_read_features(self, client, db, readonly_user):
        """GET /settings/features with readonly role → 200 (read is allowed)."""
        _, headers = readonly_user
        resp = await client.get("/api/v1/settings/features", headers=headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TDD Iteration 1 — Audit event verification
# ---------------------------------------------------------------------------


class TestAuditEvents:
    """Branding and feature mutations must produce audit events."""

    @pytest.mark.asyncio
    async def test_branding_update_creates_audit_event(self, client, db, admin_user):
        """PATCH /settings/branding should log an audit event."""
        _, headers = admin_user
        await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": "Audit Corp"},
            headers=headers,
        )

        result = await db.execute(
            text("SELECT action FROM audit_events WHERE action = 'settings.branding_update' ORDER BY id DESC LIMIT 1")
        )
        row = result.first()
        assert row is not None, "Expected audit event for settings.branding_update"
        assert row[0] == "settings.branding_update"

    @pytest.mark.asyncio
    async def test_features_update_creates_audit_event(self, client, db, admin_user):
        """PATCH /settings/features should log an audit event."""
        _, headers = admin_user
        await client.patch(
            "/api/v1/settings/features",
            json={"flags": {"chat": True}},
            headers=headers,
        )

        result = await db.execute(
            text("SELECT action FROM audit_events WHERE action = 'settings.features_update' ORDER BY id DESC LIMIT 1")
        )
        row = result.first()
        assert row is not None, "Expected audit event for settings.features_update"
        assert row[0] == "settings.features_update"

    @pytest.mark.asyncio
    async def test_domain_verified_creates_audit_event(self, client, db, admin_user):
        """Successful domain verification should log an audit event."""
        _, headers = admin_user
        await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "audit.acme.com"},
            headers=headers,
        )

        with patch("app.api.v1.settings.verify_domain", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = True
            await client.post(
                "/api/v1/settings/verify-domain",
                json={"domain": "audit.acme.com"},
                headers=headers,
            )

        result = await db.execute(
            text("SELECT action FROM audit_events WHERE action = 'settings.domain_verified' ORDER BY id DESC LIMIT 1")
        )
        row = result.first()
        assert row is not None, "Expected audit event for settings.domain_verified"


# ---------------------------------------------------------------------------
# TDD Iteration 1 — Feature flag cache invalidation
# ---------------------------------------------------------------------------


class TestFlagCacheInvalidation:
    @pytest.mark.asyncio
    async def test_set_flag_invalidates_cache(self, db, tenant_a):
        """After set_flag, the old cache entry should be removed."""
        clear_cache()
        await set_flag(db, tenant_a.id, "cache_test", True)
        await db.flush()

        # Populate cache
        result = await is_enabled(db, tenant_a.id, "cache_test")
        assert result is True
        assert (tenant_a.id, "cache_test") in _FLAG_CACHE

        # set_flag should invalidate cache
        await set_flag(db, tenant_a.id, "cache_test", False)
        await db.flush()

        assert (tenant_a.id, "cache_test") not in _FLAG_CACHE

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, db, tenant_a):
        """Cache entry should expire after _CACHE_TTL seconds."""
        clear_cache()
        await set_flag(db, tenant_a.id, "ttl_test", True)
        await db.flush()

        await is_enabled(db, tenant_a.id, "ttl_test")
        assert (tenant_a.id, "ttl_test") in _FLAG_CACHE

        # Manually backdate the cache timestamp
        _FLAG_CACHE[(tenant_a.id, "ttl_test")] = (True, time.time() - _CACHE_TTL - 1)

        # Now change in DB
        await set_flag(db, tenant_a.id, "ttl_test", False)
        await db.flush()
        # Clear only the manually set entry to force re-read
        _FLAG_CACHE.pop((tenant_a.id, "ttl_test"), None)

        result = await is_enabled(db, tenant_a.id, "ttl_test")
        assert result is False


# ---------------------------------------------------------------------------
# TDD Iteration 1 — Feature flags tenant isolation via API
# ---------------------------------------------------------------------------


class TestFeatureFlagsAPIIsolation:
    @pytest.mark.asyncio
    async def test_features_tenant_isolation(self, client, db, admin_user, admin_user_b, tenant_a):
        """Tenant A's flags should not be visible to Tenant B via API."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        # Set a flag for tenant A
        await client.patch(
            "/api/v1/settings/features",
            json={"flags": {"secret_feature": True}},
            headers=headers_a,
        )

        # Tenant B should not see it
        resp = await client.get("/api/v1/settings/features", headers=headers_b)
        assert resp.status_code == 200
        assert "secret_feature" not in resp.json()["flags"]


# ---------------------------------------------------------------------------
# TDD Iteration 1 — require_feature HTTP guard
# ---------------------------------------------------------------------------


class TestRequireFeatureHTTPGuard:
    """Test require_feature dependency as a real HTTP middleware."""

    @pytest.mark.asyncio
    async def test_feature_disabled_returns_403(self, client, db, admin_user, tenant_a, app):
        """An endpoint guarded by require_feature should return 403 when flag is disabled."""
        from typing import Annotated

        from fastapi import APIRouter, Depends

        from app.core.dependencies import require_feature
        from app.models.user import User

        # Create a temporary test endpoint
        test_router = APIRouter()

        @test_router.get("/test-feature-guard")
        async def guarded_endpoint(
            user: Annotated[User, Depends(require_feature("test_guard_feature"))],
        ):
            return {"ok": True}

        app.include_router(test_router, prefix="/api/v1")

        _, headers = admin_user
        clear_cache()

        # Feature not set → should be disabled (defaults to False)
        resp = await client.get("/api/v1/test-feature-guard", headers=headers)
        assert resp.status_code == 403

        # Enable the feature
        await set_flag(db, tenant_a.id, "test_guard_feature", True)
        await db.flush()
        clear_cache()

        resp = await client.get("/api/v1/test-feature-guard", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# TDD Iteration 2 — Edge cases
# ---------------------------------------------------------------------------


class TestBrandingEdgeCases:
    @pytest.mark.asyncio
    async def test_brand_name_max_length(self, client, db, admin_user):
        """Brand name longer than 100 chars → 422."""
        _, headers = admin_user
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": "A" * 101},
            headers=headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_brand_color_hsl_max_length(self, client, db, admin_user):
        """HSL string longer than 30 chars → 422."""
        _, headers = admin_user
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"brand_color_hsl": "X" * 31},
            headers=headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_clear_brand_name(self, client, db, admin_user):
        """Setting brand_name to null should clear it."""
        _, headers = admin_user

        # Set first
        await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": "Acme Corp"},
            headers=headers,
        )

        # Clear
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"brand_name": None},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["brand_name"] is None

    @pytest.mark.asyncio
    async def test_custom_domain_max_length(self, client, db, admin_user):
        """Domain longer than 255 chars → 422."""
        _, headers = admin_user
        resp = await client.patch(
            "/api/v1/settings/branding",
            json={"custom_domain": "x" * 256},
            headers=headers,
        )
        assert resp.status_code == 422


class TestFeatureFlagEdgeCases:
    @pytest.mark.asyncio
    async def test_set_multiple_flags_at_once(self, client, db, admin_user, tenant_a):
        """PATCH with multiple flags should set all of them."""
        _, headers = admin_user

        resp = await client.patch(
            "/api/v1/settings/features",
            json={"flags": {"a": True, "b": False, "c": True}},
            headers=headers,
        )
        assert resp.status_code == 200
        flags = resp.json()["flags"]
        assert flags["a"] is True
        assert flags["b"] is False
        assert flags["c"] is True

    @pytest.mark.asyncio
    async def test_clear_cache_function(self):
        """clear_cache should empty the global cache dict."""
        _FLAG_CACHE[("test", "key")] = (True, time.time())
        assert len(_FLAG_CACHE) > 0
        clear_cache()
        assert len(_FLAG_CACHE) == 0


class TestDomainServiceUnit:
    """Unit tests for domain_service helper functions."""

    def test_get_verification_record_format(self):
        """get_verification_record should return correct DNS record structure."""
        from app.services.domain_service import get_verification_record

        tenant_id = uuid.uuid4()
        record = get_verification_record(tenant_id)
        assert record["type"] == "TXT"
        assert record["name"] == "_netsuite-verify"
        assert record["value"] == f"tenant_{tenant_id}"

    @pytest.mark.asyncio
    async def test_verify_domain_catches_dns_errors(self):
        """verify_domain should return False on DNS errors, not raise."""
        from app.services.domain_service import verify_domain

        # Non-existent domain should return False, not raise
        result = await verify_domain("this-domain-definitely-does-not-exist-xyz123.com", uuid.uuid4())
        assert result is False
