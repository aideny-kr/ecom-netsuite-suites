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


class TestDsn:
    def test_strips_asyncpg_dialect(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL_DIRECT", _DUMMY_DSN_ASYNCPG)
        assert mrs._dsn() == _DUMMY_DSN_PLAIN

    def test_requires_env(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit):
            mrs._dsn()


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


class TestVerdict:
    def test_pass(self):
        code, msg = mrs._verdict(x_ok=False, x_state="42501", s_ok=True, s_state=None)
        assert code == 0
        assert msg.startswith("PASS")

    def test_cross_tenant_accepted_is_fail(self):
        code, msg = mrs._verdict(x_ok=True, x_state=None, s_ok=True, s_state=None)
        assert code == 1
        assert "NOT enforced" in msg

    def test_positive_control_fk_failure_is_inconclusive(self):
        code, msg = mrs._verdict(x_ok=False, x_state="42501", s_ok=False, s_state="23503")
        assert code == 2
        assert "RLS_CTX_TENANT" in msg

    def test_positive_control_rls_rejection_is_too_strict_fail(self):
        code, msg = mrs._verdict(x_ok=False, x_state="42501", s_ok=False, s_state="42501")
        assert code == 1
        assert "too strict" in msg

    def test_positive_control_unexpected_error_is_inconclusive(self):
        code, _ = mrs._verdict(x_ok=False, x_state="42501", s_ok=False, s_state="23502")
        assert code == 2

    def test_cross_tenant_rejected_with_wrong_sqlstate_is_inconclusive(self):
        code, msg = mrs._verdict(x_ok=False, x_state="23503", s_ok=True, s_state=None)
        assert code == 2
        assert "23503" in msg
