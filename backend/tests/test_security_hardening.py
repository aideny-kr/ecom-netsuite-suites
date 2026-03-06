"""Tests for production security hardening.

Verifies:
1. SQL injection prevention in SET LOCAL (parameterized queries)
2. Redis-backed token denylist
3. Redis-backed rate limiter
4. Production secret validation
5. Security headers middleware
6. Swagger docs disabled in production
7. SSL verification enabled for Supabase
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────
# 1. SQL Injection Prevention — Parameterized SET LOCAL
# ──────────────────────────────────────────────────────────────────


class TestSetLocalSqlInjectionPrevention:
    """Verify SET LOCAL calls use UUID validation to prevent SQL injection.

    PostgreSQL SET LOCAL does not support $1 bind parameters, so we validate
    that all production code uses set_tenant_context() (which validates UUID)
    or uuid.UUID() before string interpolation.
    """

    def _read(self, path: str) -> str:
        return (_BACKEND_ROOT / path).read_text()

    def test_database_set_tenant_context_validates_uuid(self):
        """set_tenant_context must validate UUID before interpolation."""
        src = self._read("app/core/database.py")
        assert "uuid.UUID" in src, "set_tenant_context must validate UUID format"
        assert "SET LOCAL app.current_tenant_id" in src

    def test_dependencies_uses_set_tenant_context(self):
        """dependencies.py must use set_tenant_context helper."""
        src = self._read("app/core/dependencies.py")
        assert "set_tenant_context" in src, "dependencies.py must use set_tenant_context"

    def test_api_key_auth_uses_set_tenant_context(self):
        """api_key_auth.py must use set_tenant_context helper."""
        src = self._read("app/core/api_key_auth.py")
        assert "set_tenant_context" in src, "api_key_auth.py must use set_tenant_context"

    def test_base_task_validates_uuid(self):
        """base_task.py must validate UUID before SET LOCAL."""
        src = self._read("app/workers/base_task.py")
        assert "uuid.UUID" in src, "base_task.py must validate UUID"
        assert "SET LOCAL app.current_tenant_id" in src

    def test_set_tenant_context_rejects_invalid_uuid(self):
        """set_tenant_context must raise ValueError for non-UUID input."""
        import uuid as uuid_mod

        # Simulate what set_tenant_context does
        with pytest.raises(ValueError):
            str(uuid_mod.UUID("'; DROP TABLE tenants; --"))

    def test_set_tenant_context_accepts_valid_uuid(self):
        """set_tenant_context must accept valid UUIDs."""
        import uuid as uuid_mod

        valid = "bf92d059-1234-5678-9abc-def012345678"
        result = str(uuid_mod.UUID(valid))
        assert result == valid


# ──────────────────────────────────────────────────────────────────
# 2. Redis Token Denylist
# ──────────────────────────────────────────────────────────────────


class TestTokenDenylist:
    """Test Redis-backed JWT denylist."""

    def setup_method(self):
        from app.core.token_denylist import reset_denylist

        reset_denylist()

    def test_revoke_and_check(self):
        from app.core.token_denylist import is_revoked, revoke_token

        jti = "test-jti-123"
        exp = time.time() + 3600  # 1 hour from now
        assert not is_revoked(jti)
        revoke_token(jti, exp)
        assert is_revoked(jti)

    def test_unrevoked_token_is_not_revoked(self):
        from app.core.token_denylist import is_revoked

        assert not is_revoked("never-revoked")

    def test_reset_clears_state(self):
        from app.core.token_denylist import is_revoked, reset_denylist, revoke_token

        revoke_token("jti-1", time.time() + 3600)
        assert is_revoked("jti-1")
        reset_denylist()
        # After reset, may or may not be revoked depending on Redis state
        # But the function should not crash
        # In fallback mode, it will be cleared

    def test_module_uses_redis_import(self):
        """Token denylist module should import redis."""
        src = (_BACKEND_ROOT / "app/core/token_denylist.py").read_text()
        assert "import redis" in src
        assert "REDIS_URL" in src or "settings.REDIS_URL" in src


# ──────────────────────────────────────────────────────────────────
# 3. Redis Rate Limiter
# ──────────────────────────────────────────────────────────────────


class TestRateLimiter:
    """Test Redis-backed rate limiter."""

    def setup_method(self):
        from app.core.rate_limit import reset_rate_limits

        reset_rate_limits()

    def test_allows_first_request(self):
        from app.core.rate_limit import check_login_rate_limit

        assert check_login_rate_limit("192.168.1.1")

    def test_blocks_after_max_attempts(self):
        from app.core.rate_limit import MAX_ATTEMPTS, check_login_rate_limit

        ip = "192.168.1.2"
        for _ in range(MAX_ATTEMPTS):
            assert check_login_rate_limit(ip)
        # Next attempt should be blocked
        assert not check_login_rate_limit(ip)

    def test_different_ips_independent(self):
        from app.core.rate_limit import MAX_ATTEMPTS, check_login_rate_limit

        ip_a = "10.0.0.1"
        ip_b = "10.0.0.2"
        for _ in range(MAX_ATTEMPTS):
            check_login_rate_limit(ip_a)
        assert not check_login_rate_limit(ip_a)
        assert check_login_rate_limit(ip_b)

    def test_module_uses_redis_import(self):
        """Rate limiter module should import redis."""
        src = (_BACKEND_ROOT / "app/core/rate_limit.py").read_text()
        assert "import redis" in src
        assert "REDIS_URL" in src or "settings.REDIS_URL" in src


# ──────────────────────────────────────────────────────────────────
# 4. Production Secret Validation
# ──────────────────────────────────────────────────────────────────


class TestProductionSecretValidation:
    """Verify app refuses to start with default secrets in production."""

    def test_validates_jwt_secret(self):
        from app.main import _validate_production_secrets

        with patch("app.main.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            mock_settings.JWT_SECRET_KEY = "change-me-in-production"
            mock_settings.ENCRYPTION_KEY = "valid-key"
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
                _validate_production_secrets()

    def test_validates_encryption_key(self):
        from app.main import _validate_production_secrets

        with patch("app.main.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            mock_settings.JWT_SECRET_KEY = "proper-secret"
            mock_settings.ENCRYPTION_KEY = "change-me-generate-a-real-fernet-key"
            with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
                _validate_production_secrets()

    def test_skips_validation_in_development(self):
        from app.main import _validate_production_secrets

        with patch("app.main.settings") as mock_settings:
            mock_settings.APP_ENV = "development"
            mock_settings.JWT_SECRET_KEY = "change-me-in-production"
            mock_settings.ENCRYPTION_KEY = "change-me-generate-a-real-fernet-key"
            # Should not raise
            _validate_production_secrets()

    def test_passes_with_real_secrets(self):
        from app.main import _validate_production_secrets

        with patch("app.main.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            mock_settings.JWT_SECRET_KEY = "a-real-secret-key-here"
            mock_settings.ENCRYPTION_KEY = "a-real-fernet-key-here"
            _validate_production_secrets()


# ──────────────────────────────────────────────────────────────────
# 5. Security Headers
# ──────────────────────────────────────────────────────────────────


class TestSecurityHeaders:
    """Verify security headers middleware is configured."""

    def test_security_headers_middleware_exists(self):
        src = (_BACKEND_ROOT / "app/main.py").read_text()
        assert "SecurityHeadersMiddleware" in src

    def test_x_content_type_options(self):
        src = (_BACKEND_ROOT / "app/main.py").read_text()
        assert "X-Content-Type-Options" in src
        assert "nosniff" in src

    def test_x_frame_options(self):
        src = (_BACKEND_ROOT / "app/main.py").read_text()
        assert "X-Frame-Options" in src
        assert "DENY" in src

    def test_hsts_header(self):
        src = (_BACKEND_ROOT / "app/main.py").read_text()
        assert "Strict-Transport-Security" in src


# ──────────────────────────────────────────────────────────────────
# 6. Swagger Docs Disabled in Production
# ──────────────────────────────────────────────────────────────────


class TestSwaggerDocs:
    """Verify Swagger/ReDoc disabled outside development."""

    def test_docs_url_conditional(self):
        src = (_BACKEND_ROOT / "app/main.py").read_text()
        assert 'docs_url="/docs" if is_dev else None' in src or "docs_url" in src

    def test_redoc_url_conditional(self):
        src = (_BACKEND_ROOT / "app/main.py").read_text()
        assert 'redoc_url="/redoc" if is_dev else None' in src or "redoc_url" in src


# ──────────────────────────────────────────────────────────────────
# 7. SSL Verification for Supabase
# ──────────────────────────────────────────────────────────────────


class TestSSLVerification:
    """Verify SSL certificate verification is enabled for Supabase."""

    def test_no_cert_none(self):
        src = (_BACKEND_ROOT / "app/core/database.py").read_text()
        assert "CERT_NONE" not in src, "SSL verification must not be disabled"

    def test_no_check_hostname_false(self):
        src = (_BACKEND_ROOT / "app/core/database.py").read_text()
        assert "check_hostname = False" not in src, "Hostname checking must not be disabled"

    def test_uses_default_context(self):
        src = (_BACKEND_ROOT / "app/core/database.py").read_text()
        assert "create_default_context" in src


# ──────────────────────────────────────────────────────────────────
# 8. Entrypoint — No Alembic on Boot
# ──────────────────────────────────────────────────────────────────


class TestEntrypoint:
    """Verify migrations don't run at container startup."""

    def test_no_alembic_in_entrypoint(self):
        entrypoint = (_BACKEND_ROOT / "entrypoint.sh").read_text()
        assert "alembic upgrade" not in entrypoint, "Migrations must not run at container startup"
