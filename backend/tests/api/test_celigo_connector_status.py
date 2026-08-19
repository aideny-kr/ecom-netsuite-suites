"""Celigo connect card — status, test, connect, disconnect.

Fixture note (deviates from the task-6 brief, which assumed fixtures named
``auth_headers``/``db_session`` that do not exist in this harness): mirrors
``tests/api/test_tenant_memory_api.py`` — ``client`` (ASGI client) is real,
but auth comes from ``admin_user`` -> ``(User, headers_dict)`` and the DB
session fixture is plain ``db`` (see ``tests/conftest.py``).
"""

from app.services.celigo.client import CeligoAuthError


class TestCeligoStatus:
    async def test_status_reports_disconnected_when_absent(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/connector-status/celigo", headers=headers)
        assert r.status_code == 200
        assert r.json()["connected"] is False

    async def test_status_requires_permission(self, client):
        r = await client.get("/api/v1/connector-status/celigo")
        assert r.status_code in (401, 403)


class TestCeligoTest:
    async def test_valid_token_reports_account(self, client, admin_user, monkeypatch):
        _, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/test",
            headers=headers,
            json={"token": "tok", "region": "us"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["account_name"] == "Framework"

    async def test_bad_token_returns_actionable_error_not_500(self, client, admin_user, monkeypatch):
        _, headers = admin_user

        async def _bad(token, region="us", **kw):
            raise CeligoAuthError("Invalid token")

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _bad, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/test",
            headers=headers,
            json={"token": "bad", "region": "us"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "Invalid token" in r.json()["error"]


class TestCeligoConnect:
    async def test_connect_stores_encrypted_token(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo Production"},
        )
        assert r.status_code == 201, r.text

        from sqlalchemy import select

        from app.models.connection import Connection

        row = (
            await db.execute(
                select(Connection).where(
                    Connection.tenant_id == user.tenant_id,
                    Connection.provider == "celigo",
                )
            )
        ).scalar_one()

        assert row.encrypted_credentials
        assert "s3cret" not in row.encrypted_credentials, "token stored in plaintext"
        assert row.metadata_json["region"] == "us"
        assert row.metadata_json["account_name"] == "Framework"
        assert "token" not in row.metadata_json, "token leaked into metadata_json"

    async def test_connect_rejects_invalid_token_before_storing(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _bad(token, region="us", **kw):
            raise CeligoAuthError("Invalid token")

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _bad, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "bad", "region": "us", "label": "x"},
        )
        assert r.status_code == 400

        from sqlalchemy import select

        from app.models.connection import Connection

        row = (
            await db.execute(
                select(Connection).where(
                    Connection.tenant_id == user.tenant_id,
                    Connection.provider == "celigo",
                )
            )
        ).scalar_one_or_none()
        assert row is None, "invalid token must never create a connection row"


class TestCeligoDisconnect:
    async def test_disconnect_soft_deletes(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)

        connect_resp = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo Production"},
        )
        assert connect_resp.status_code == 201, connect_resp.text

        r = await client.delete("/api/v1/connector-status/celigo", headers=headers)
        assert r.status_code == 204

        from sqlalchemy import select

        from app.models.connection import Connection

        row = (
            await db.execute(
                select(Connection).where(
                    Connection.tenant_id == user.tenant_id,
                    Connection.provider == "celigo",
                )
            )
        ).scalar_one()
        assert row.status == "revoked", "disconnect must soft-delete, not hard-delete"

    async def test_disconnect_requires_permission(self, client):
        r = await client.delete("/api/v1/connector-status/celigo")
        assert r.status_code in (401, 403)
