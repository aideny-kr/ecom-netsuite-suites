"""Celigo connect card — status, test, connect, disconnect.

Fixture note (deviates from the task-6 brief, which assumed fixtures named
``auth_headers``/``db_session`` that do not exist in this harness): mirrors
``tests/api/test_tenant_memory_api.py`` — ``client`` (ASGI client) is real,
but auth comes from ``admin_user`` -> ``(User, headers_dict)`` and the DB
session fixture is plain ``db`` (see ``tests/conftest.py``).
"""

import httpx
import pytest

from app.services.celigo.client import CeligoAuthError, CeligoError
from tests.conftest import enable_feature_flag


@pytest.fixture(autouse=True)
async def _celigo_flag_enabled(db, admin_user):
    """MAJOR 4 (T2 gate on PR #202): all four Celigo endpoints below are now
    gated on the `celigo` feature flag (default OFF). Enable it here for
    every test in this file so the rest of the suite continues to exercise
    normal (flag-on) behavior; TestCeligoFeatureFlag below explicitly turns
    it back off to prove the 403 path."""
    user, _ = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")


class TestCeligoFeatureFlag:
    """The flag is the kill switch for the whole Celigo surface -- a
    default-off flag that leaves the API live for every tenant is not one."""

    async def test_status_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)

        r = await client.get("/api/v1/connector-status/celigo", headers=headers)
        assert r.status_code == 403

    async def test_test_endpoint_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/test",
            headers=headers,
            json={"token": "tok", "region": "us"},
        )
        assert r.status_code == 403

    async def test_connect_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "tok", "region": "us", "label": "x"},
        )
        assert r.status_code == 403

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
        assert row is None, "a disabled flag must never allow a connection row to be created"

    async def test_disconnect_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)

        r = await client.delete("/api/v1/connector-status/celigo", headers=headers)
        assert r.status_code == 403


class TestCeligoStatus:
    async def test_status_reports_disconnected_when_absent(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/connector-status/celigo", headers=headers)
        assert r.status_code == 200
        assert r.json()["connected"] is False

    async def test_status_requires_permission(self, client):
        r = await client.get("/api/v1/connector-status/celigo")
        assert r.status_code in (401, 403)


class TestCeligoRegionValidation:
    """CeligoTestRequest/CeligoConnectRequest.region were bare `str`, and
    client.base_url() silently falls back to US on any unknown value. So
    region="EU" (capitalized) routed to the US host, the EU token got rejected,
    and the operator saw a 400 "invalid token" -- blaming them for a routing
    bug. Constraining to Literal["us", "eu"] turns a bad value into a clear
    422 at the boundary instead."""

    async def test_test_endpoint_rejects_invalid_region(self, client, admin_user):
        _, headers = admin_user
        r = await client.post(
            "/api/v1/connector-status/celigo/test",
            headers=headers,
            json={"token": "tok", "region": "EU"},
        )
        assert r.status_code == 422, r.text

    async def test_connect_endpoint_rejects_invalid_region(self, client, admin_user, db):
        user, headers = admin_user
        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "tok", "region": "EU", "label": "x"},
        )
        assert r.status_code == 422, r.text

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
        assert row is None, "a rejected region must never create a connection row"


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

    async def test_requires_connections_manage_not_just_view(self, client, readonly_user):
        """A viewer (connections.view only, no connections.manage) must not be able to
        use /test as an oracle to probe arbitrary Celigo tokens."""
        _, headers = readonly_user

        r = await client.post(
            "/api/v1/connector-status/celigo/test",
            headers=headers,
            json={"token": "tok", "region": "us"},
        )
        assert r.status_code == 403


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

    async def test_connect_returns_502_on_celigo_error_not_500(self, client, admin_user, db, monkeypatch):
        """A Celigo-side 5xx/429/unexpected-status must surface as an actionable 502,
        not an opaque 500 -- and must not persist a row."""
        user, headers = admin_user

        async def _outage(token, region="us", **kw):
            raise CeligoError("Celigo returned 503")

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _outage, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "tok", "region": "us", "label": "x"},
        )
        assert r.status_code == 502, r.text
        assert "Celigo returned 503" in r.json()["detail"]

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
        assert row is None, "an upstream outage must never create a connection row"

    async def test_connect_returns_502_on_network_failure_not_500(self, client, admin_user, db, monkeypatch):
        """A raw httpx transport failure (timeout/DNS/connect-refused) -- unwrapped by
        the Celigo client -- must also surface as 502, not crash to 500."""
        user, headers = admin_user

        async def _timeout(token, region="us", **kw):
            raise httpx.ConnectTimeout("connect timed out")

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _timeout, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "tok", "region": "us", "label": "x"},
        )
        assert r.status_code == 502, r.text

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
        assert row is None, "a network failure must never create a connection row"


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


class TestCeligoAgentAccess:
    """Task 10 — /celigo/connect also creates a celigo_mcp McpConnector when an
    agent_token is supplied, so the "Agent access" field stops being silently
    discarded and the chat agent actually gets Celigo read tools.
    """

    async def test_connect_without_agent_token_creates_no_mcp_row(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["agent_access"] is False

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        row = (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == user.tenant_id,
                    McpConnector.provider == "celigo_mcp",
                )
            )
        ).scalar_one_or_none()
        assert row is None, "connecting without agent_token must not create an MCP row"

    async def test_connect_with_agent_token_creates_enabled_mcp_row(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return [{"name": "list_flows", "description": "List flows"}]

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["agent_access"] is True

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        rows = (
            (
                await db.execute(
                    select(McpConnector).where(
                        McpConnector.tenant_id == user.tenant_id,
                        McpConnector.provider == "celigo_mcp",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.is_enabled is True
        assert row.server_url == "https://api.integrator.io/celigo-mcp"
        assert row.auth_type == "bearer"

    async def test_agent_token_never_leaks_plaintext_or_metadata(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return []

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-s3cret"},
        )
        assert r.status_code == 201, r.text
        assert "agent-s3cret" not in r.text, "the agent token must never appear in the response body"

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        row = (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == user.tenant_id,
                    McpConnector.provider == "celigo_mcp",
                )
            )
        ).scalar_one()
        assert "agent-s3cret" not in row.encrypted_credentials, "agent token stored in plaintext"
        assert row.metadata_json is None or "agent-s3cret" not in str(row.metadata_json), (
            "agent token leaked into metadata_json"
        )

    async def test_disconnect_revokes_mcp_connector_too(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return []

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        connect_resp = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert connect_resp.status_code == 201, connect_resp.text

        r = await client.delete("/api/v1/connector-status/celigo", headers=headers)
        assert r.status_code == 204

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        row = (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == user.tenant_id,
                    McpConnector.provider == "celigo_mcp",
                )
            )
        ).scalar_one()
        assert row.status == "revoked", "disconnect must also revoke the celigo_mcp connector"
        assert row.is_enabled is False

    async def test_reconnect_after_disconnect_reactivates_not_duplicates(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return []

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        await client.delete("/api/v1/connector-status/celigo", headers=headers)
        reconnect = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret2", "region": "us", "label": "Celigo", "agent_token": "agent-tok2"},
        )
        assert reconnect.status_code == 201, reconnect.text
        assert reconnect.json()["agent_access"] is True

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        rows = (
            (
                await db.execute(
                    select(McpConnector).where(
                        McpConnector.tenant_id == user.tenant_id,
                        McpConnector.provider == "celigo_mcp",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "reconnect must reactivate the existing row, not create a duplicate"
        assert rows[0].status == "active"
        assert rows[0].is_enabled is True

    async def test_mcp_creation_failure_does_not_fail_rest_connection(self, client, admin_user, db, monkeypatch):
        """An agent-access failure must never fail the REST connection."""
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_connector_service.create_mcp_connector", _boom, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["connected"] is True
        assert r.json()["agent_access"] is False

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
        assert row.status == "active", "the REST connection must still succeed despite the MCP-side failure"

    async def test_status_reports_agent_access_truthfully(self, client, admin_user, monkeypatch):
        _, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return []

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        r0 = await client.get("/api/v1/connector-status/celigo", headers=headers)
        assert r0.json()["agent_access"] is False

        await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        r1 = await client.get("/api/v1/connector-status/celigo", headers=headers)
        assert r1.json()["agent_access"] is True

    async def test_discovery_failure_leaves_agent_access_false_but_rest_connection_ok(
        self, client, admin_user, db, monkeypatch
    ):
        """MAJOR 3 (T2 gate): agent_token is never itself verified -- only
        discover_tools proves the token actually yields usable Celigo tools.
        Before this fix, is_enabled/status were set to active unconditionally
        and a discovery failure was swallowed, so `agent_access: true` could
        mean "a row exists" rather than "the agent actually has tools"."""
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discovery_fails(connector, db=None):
            raise RuntimeError("celigo mcp server unreachable")

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discovery_fails, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["connected"] is True, "REST connection must still succeed"
        assert r.json()["agent_access"] is False, "discovery failure must not report agent access"

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        row = (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == user.tenant_id,
                    McpConnector.provider == "celigo_mcp",
                )
            )
        ).scalar_one()
        assert row.is_enabled is False, "a connector with zero verified tools must not be enabled"
        assert not row.discovered_tools, "no tools were ever verified"

        status_resp = await client.get("/api/v1/connector-status/celigo", headers=headers)
        assert status_resp.json()["agent_access"] is False

    async def test_reconnect_with_failing_discovery_clears_stale_tools(self, client, admin_user, db, monkeypatch):
        """MINOR (T2 gate): a reconnect whose discovery fails must not leave a
        PREVIOUS successful discovery's tool list attached to a token that
        just failed to enumerate tools."""
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover_ok(connector, db=None):
            return [{"name": "list_flows", "description": "List flows"}]

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover_ok, raising=False)

        first = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert first.status_code == 201, first.text
        assert first.json()["agent_access"] is True

        async def _discover_fails(connector, db=None):
            raise RuntimeError("celigo mcp server unreachable")

        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover_fails, raising=False)

        second = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret2", "region": "us", "label": "Celigo", "agent_token": "agent-tok2"},
        )
        assert second.status_code == 201, second.text
        assert second.json()["agent_access"] is False

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector

        row = (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == user.tenant_id,
                    McpConnector.provider == "celigo_mcp",
                )
            )
        ).scalar_one()
        assert not row.discovered_tools, "the earlier successful discovery's tools must be cleared"
        assert row.is_enabled is False


class TestCeligoMcpGuardIntegration:
    """End-to-end proof that Tasks 1-3's read-only guards protect the real
    celigo_mcp connector Task 10 creates, not just a hypothetical one."""

    async def test_guards_protect_the_created_connector(self, client, admin_user, db, monkeypatch):
        user, headers = admin_user

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return [
                {"name": "list_flows", "description": "List flows"},
                {"name": "delete_resource", "description": "Delete a resource"},
            ]

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert r.status_code == 201, r.text

        from sqlalchemy import select

        from app.models.mcp_connector import McpConnector
        from app.services.chat import tools as tools_mod

        connector = (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == user.tenant_id,
                    McpConnector.provider == "celigo_mcp",
                )
            )
        ).scalar_one()

        # Layer 1 -- write tools never enter the model's inventory.
        names = [t["name"] for t in tools_mod.build_external_tool_definitions([connector])]
        assert any(n.endswith("__list_flows") for n in names)
        assert not any(n.endswith("__delete_resource") for n in names)

        # Layer 2 -- the dispatcher refuses a write even called directly.
        result = await tools_mod._execute_external_tool(
            connector_id=connector.id,
            raw_tool_name="delete_resource",
            tool_input={},
            tenant_id=user.tenant_id,
            db=db,
        )
        assert "error" in result
        assert "read-only" in result["error"].lower()


class TestCeligoTenantContextAfterCommit:
    """Whole-branch review fix: `db.commit()` inside connect_celigo (the REST
    connection write) is a REAL commit on the request-scoped session, and
    ``SET LOCAL app.current_tenant_id`` is transaction-scoped -- it dies at that
    commit. Everything connect_celigo does afterward (db.refresh(connection),
    the celigo_mcp upsert + its audit log, or -- when no agent_token is given --
    the `_celigo_agent_access` lookup) runs with NO tenant context unless it is
    re-established first.

    Not broken today only because mcp_connectors/audit_logs aren't FORCE-RLS'd
    and every query here also filters by tenant_id in application code. It
    activates silently (inside the `except Exception` around the celigo_mcp
    block) the moment either table is FORCE-RLS'd.

    Per tests/conftest.py's fixture note: the SAVEPOINT-based test transaction
    means a service-internal commit here is a RELEASE SAVEPOINT, not a real
    COMMIT, so it does NOT actually clear the GUC in this harness -- asserting
    GUC state would false-pass. These tests instead assert CALL ORDERING of
    set_tenant_context against commit/refresh/the agent-access read, mirroring
    test_report_refresh.py's ctx-spy pattern.
    """

    def _spy(self, monkeypatch, db):
        from app.core import database as database_module

        events: list = []
        real_commit = db.commit
        real_refresh = db.refresh
        real_set_ctx = database_module.set_tenant_context

        async def spy_ctx(session, tenant_id):
            events.append("ctx")
            await real_set_ctx(session, tenant_id)

        async def spy_commit():
            events.append("commit")
            await real_commit()

        async def spy_refresh(*a, **kw):
            events.append("refresh")
            return await real_refresh(*a, **kw)

        # raising=False: the pre-fix module doesn't import set_tenant_context at
        # all, so this attribute doesn't exist yet -- that must fail the ordering
        # assertions below (no "ctx" event), not an unrelated AttributeError.
        monkeypatch.setattr("app.api.v1.connector_status.set_tenant_context", spy_ctx, raising=False)
        monkeypatch.setattr(db, "commit", spy_commit)
        monkeypatch.setattr(db, "refresh", spy_refresh)
        return events

    async def test_context_reestablished_before_refresh_and_mcp_work(self, client, admin_user, db, monkeypatch):
        """agent_token path: ctx must be re-set between the connection commit and
        BOTH db.refresh(connection) and the celigo_mcp upsert/audit block."""
        user, headers = admin_user
        events = self._spy(monkeypatch, db)

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        async def _discover(connector, db=None):
            return []

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.discover_tools", _discover, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo", "agent_token": "agent-tok"},
        )
        assert r.status_code == 201, r.text

        assert "commit" in events
        first_commit = events.index("commit")
        assert "ctx" in events[first_commit:], "tenant context must be re-established after the connection commit"
        ctx_after_commit = events.index("ctx", first_commit)
        assert "refresh" in events[ctx_after_commit:], (
            "db.refresh(connection) must run under the re-established context"
        )
        refresh_idx = events.index("refresh", ctx_after_commit)
        assert ctx_after_commit < refresh_idx, "context must be re-set BEFORE db.refresh, not after"

    async def test_context_reestablished_before_agent_access_read_without_agent_token(
        self, client, admin_user, db, monkeypatch
    ):
        """No agent_token: the `else` branch's `_celigo_agent_access` SELECT is
        also DB work after the same commit and needs the same re-establishment."""
        from app.services import mcp_connector_service

        user, headers = admin_user
        events = self._spy(monkeypatch, db)
        real_get_active = mcp_connector_service.get_active_connectors_for_tenant

        async def spy_get_active(db, tenant_id):
            events.append("agent_access_read")
            return await real_get_active(db, tenant_id)

        monkeypatch.setattr(
            "app.api.v1.connector_status.mcp_connector_service.get_active_connectors_for_tenant",
            spy_get_active,
        )

        async def _ok(token, region="us", **kw):
            return {"account_name": "Framework", "user_email": "ops@frame.work"}

        monkeypatch.setattr("app.api.v1.connector_status.verify_token", _ok, raising=False)

        r = await client.post(
            "/api/v1/connector-status/celigo/connect",
            headers=headers,
            json={"token": "s3cret", "region": "us", "label": "Celigo"},
        )
        assert r.status_code == 201, r.text

        assert "commit" in events
        first_commit = events.index("commit")
        ctx_after_commit = events.index("ctx", first_commit)
        read_idx = events.index("agent_access_read")
        assert ctx_after_commit < read_idx, "tenant context must be re-set before the agent-access read"
