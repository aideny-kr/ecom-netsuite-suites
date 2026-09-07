"""Unit tests for the pure helpers in scripts/uat/metric_rls_smoke.py (no DB needed).

The script is a standalone UAT harness (not part of the app package), so it is
loaded by file path. Only the decision logic is tested here; the asyncpg I/O is
exercised by the live staging run documented in scripts/uat/README.md.
"""

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "uat" / "metric_rls_smoke.py"
_spec = importlib.util.spec_from_file_location("metric_rls_smoke", _SCRIPT)
mrs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrs)

# Password-free on purpose (secret-scan hooks); the helper only rewrites the dialect prefix.
_DUMMY_DSN_ASYNCPG = "postgresql+asyncpg://smoke_user@db.example.invalid:5432/postgres"
_DUMMY_DSN_PLAIN = "postgresql://smoke_user@db.example.invalid:5432/postgres"

_RLS_MSG = 'new row violates row-level security policy for table "metric_definitions"'
_PRIV_MSG = "permission denied for table metric_definitions"


class TestDsn:
    def test_strips_asyncpg_dialect(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL_DIRECT", _DUMMY_DSN_ASYNCPG)
        assert mrs._dsn() == _DUMMY_DSN_PLAIN

    def test_missing_env_exits_inconclusive(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as exc:
            mrs._dsn()
        assert exc.value.code == 2  # misconfig is INCONCLUSIVE, never FAIL

    def test_no_silent_fallback_to_database_url(self, monkeypatch):
        # Targeting staging must be deliberate — plain DATABASE_URL (often local/CI) is refused.
        monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
        monkeypatch.setenv("DATABASE_URL", _DUMMY_DSN_PLAIN)
        with pytest.raises(SystemExit) as exc:
            mrs._dsn()
        assert exc.value.code == 2


class TestProbeRole:
    def test_defaults_to_authenticated(self, monkeypatch):
        monkeypatch.delenv("RLS_SET_ROLE", raising=False)
        assert mrs._probe_role() == "authenticated"

    def test_empty_disables_set_role(self, monkeypatch):
        monkeypatch.setenv("RLS_SET_ROLE", "")
        assert mrs._probe_role() is None

    def test_custom_role(self, monkeypatch):
        monkeypatch.setenv("RLS_SET_ROLE", "app_rls_probe")
        assert mrs._probe_role() == "app_rls_probe"

    @pytest.mark.parametrize(
        "bad",
        [
            'authenticated"; drop table x;--',
            "role name",
            "Authenticated",  # quoted-upper would be a different role; refuse rather than guess
            "1role",
            "role;",
        ],
    )
    def test_rejects_unsafe_identifiers(self, monkeypatch, bad):
        monkeypatch.setenv("RLS_SET_ROLE", bad)
        with pytest.raises(ValueError):
            mrs._probe_role()


class TestTenants:
    _FRAMEWORK = "ce3dfaad-626f-4992-84e9-500c8291ca0a"

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("RLS_CTX_TENANT", raising=False)
        monkeypatch.delenv("RLS_OTHER_TENANT", raising=False)
        assert mrs._tenants() == (mrs.UAT_SMOKE_TENANT, mrs.SYSTEM_TENANT)

    @pytest.mark.parametrize(
        "spelling",
        [
            "CE3DFAAD-626F-4992-84E9-500C8291CA0A",
            "{ce3dfaad-626f-4992-84e9-500c8291ca0a}",
            "ce3dfaad626f499284e9500c8291ca0a",
            "urn:uuid:ce3dfaad-626f-4992-84e9-500c8291ca0a",
        ],
    )
    def test_normalizes_to_canonical_form(self, monkeypatch, spelling):
        # The normalized form is what gets interpolated into SET LOCAL (matches
        # set_tenant_context's str(uuid.UUID(...)) normalize-before-interpolate).
        monkeypatch.setenv("RLS_CTX_TENANT", spelling)
        monkeypatch.delenv("RLS_OTHER_TENANT", raising=False)
        ctx, _ = mrs._tenants()
        assert ctx == self._FRAMEWORK

    def test_same_tenant_in_different_spellings_is_rejected(self, monkeypatch):
        monkeypatch.setenv("RLS_CTX_TENANT", self._FRAMEWORK)
        monkeypatch.setenv("RLS_OTHER_TENANT", self._FRAMEWORK.upper())
        with pytest.raises(ValueError):
            mrs._tenants()

    def test_bad_uuid_rejected(self, monkeypatch):
        monkeypatch.setenv("RLS_CTX_TENANT", "not-a-uuid")
        with pytest.raises(ValueError):
            mrs._tenants()


class TestIsRlsRejection:
    def test_rls_message_with_42501_is_rls(self):
        assert mrs._is_rls_rejection("42501", _RLS_MSG) is True

    def test_plain_privilege_denial_is_not_rls(self):
        # Same SQLSTATE, different cause: missing INSERT grant / schema USAGE.
        assert mrs._is_rls_rejection("42501", _PRIV_MSG) is False

    def test_other_sqlstate_is_not_rls(self):
        assert mrs._is_rls_rejection("23503", _RLS_MSG) is False

    def test_none_message_is_not_rls(self):
        assert mrs._is_rls_rejection("42501", None) is False


class TestVerdict:
    def test_pass(self):
        code, msg = mrs._verdict(x_ok=False, x_state="42501", s_ok=True, s_state=None, x_msg=_RLS_MSG)
        assert code == 0
        assert msg.startswith("PASS")

    def test_cross_tenant_accepted_is_fail(self):
        code, msg = mrs._verdict(x_ok=True, x_state=None, s_ok=True, s_state=None)
        assert code == 1
        assert "NOT enforced" in msg

    def test_cross_tenant_accepted_dominates_failed_positive_control(self):
        # x_ok=True is unconditional proof of non-enforcement — a broken positive
        # control (e.g. typo'd RLS_CTX_TENANT hitting FK) must NOT bury it in exit 2.
        code, msg = mrs._verdict(x_ok=True, x_state=None, s_ok=False, s_state="23503")
        assert code == 1
        assert "NOT enforced" in msg

    def test_cross_tenant_accepted_dominates_same_tenant_rls_rejection(self):
        # Inverted/mis-keyed policy: worst possible state — must surface the
        # cross-tenant-accepted evidence, not just "too strict".
        code, msg = mrs._verdict(x_ok=True, x_state=None, s_ok=False, s_state="42501", s_msg=_RLS_MSG)
        assert code == 1
        assert "NOT enforced" in msg

    def test_positive_control_fk_failure_is_inconclusive(self):
        code, msg = mrs._verdict(x_ok=False, x_state="42501", s_ok=False, s_state="23503", x_msg=_RLS_MSG)
        assert code == 2
        assert "RLS_CTX_TENANT" in msg

    def test_positive_control_rls_rejection_is_too_strict_fail(self):
        code, msg = mrs._verdict(
            x_ok=False, x_state="42501", s_ok=False, s_state="42501", x_msg=_RLS_MSG, s_msg=_RLS_MSG
        )
        assert code == 1
        assert "too strict" in msg

    def test_positive_control_plain_privilege_denial_is_inconclusive(self):
        # 42501 without the RLS message = missing grant, NOT a policy problem.
        code, msg = mrs._verdict(
            x_ok=False, x_state="42501", s_ok=False, s_state="42501", x_msg=_PRIV_MSG, s_msg=_PRIV_MSG
        )
        assert code == 2
        assert "privilege" in msg.lower() or "grant" in msg.lower()

    def test_positive_control_unexpected_error_is_inconclusive(self):
        code, _ = mrs._verdict(x_ok=False, x_state="42501", s_ok=False, s_state="23502", x_msg=_RLS_MSG)
        assert code == 2

    def test_cross_tenant_rejected_with_wrong_sqlstate_is_inconclusive(self):
        code, msg = mrs._verdict(x_ok=False, x_state="23503", s_ok=True, s_state=None)
        assert code == 2
        assert "23503" in msg

    def test_cross_tenant_plain_privilege_denial_is_inconclusive(self):
        code, msg = mrs._verdict(x_ok=False, x_state="42501", s_ok=True, s_state=None, x_msg=_PRIV_MSG)
        assert code == 2
        assert "privilege" in msg.lower() or "grant" in msg.lower()


class TestPgErrorLine:
    def test_truncates_first_line(self):
        assert mrs._pg_error_line(ValueError("first\nsecond")) == "first"

    def test_empty_message_does_not_raise(self):
        assert mrs._pg_error_line(ValueError("")) == ""
