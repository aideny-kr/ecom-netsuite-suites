"""Tests for connection CRUD and credential encryption."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


class TestConnectionCRUD:

    async def test_create_connection(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "shopify",
            "label": "My Shopify Store",
            "credentials": {"api_key": "shppa_abc123", "api_secret": "secret456"},
        }, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["provider"] == "shopify"
        assert data["label"] == "My Shopify Store"
        assert data["status"] == "active"
        assert "encrypted_credentials" not in data

    async def test_create_connection_invalid_provider(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "invalid_provider",
            "label": "Bad Provider",
            "credentials": {"key": "val"},
        }, headers=headers)
        assert resp.status_code == 422

    async def test_list_connections_no_secrets(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        # Create a connection
        await client.post("/api/v1/connections", json={
            "provider": "stripe",
            "label": "Stripe Prod",
            "credentials": {"api_key": "sk_live_supersecret"},
        }, headers=headers)

        resp = await client.get("/api/v1/connections", headers=headers)
        assert resp.status_code == 200
        connections = resp.json()
        assert len(connections) >= 1
        for conn in connections:
            assert "encrypted_credentials" not in conn
            assert "credentials" not in conn

    async def test_delete_connection(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "netsuite",
            "label": "NS Prod",
            "credentials": {"account_id": "12345", "token": "abc"},
        }, headers=headers)
        conn_id = resp.json()["id"]

        resp_del = await client.delete(f"/api/v1/connections/{conn_id}", headers=headers)
        assert resp_del.status_code == 204

    async def test_delete_nonexistent_connection(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        fake_id = str(uuid.uuid4())
        resp = await client.delete(f"/api/v1/connections/{fake_id}", headers=headers)
        assert resp.status_code == 404

    async def test_test_connection_stub(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "shopify",
            "label": "Test Connection",
            "credentials": {"key": "val"},
        }, headers=headers)
        conn_id = resp.json()["id"]

        resp_test = await client.post(f"/api/v1/connections/{conn_id}/test", headers=headers)
        assert resp_test.status_code == 200
        assert resp_test.json()["status"] == "ok"


class TestCredentialEncryption:
    """Verify credentials are encrypted at rest."""

    async def test_credentials_encrypted_in_db(self, client: AsyncClient, admin_user, db: AsyncSession):
        _, headers = admin_user
        plaintext_key = "sk_live_supersecretkey123"
        resp = await client.post("/api/v1/connections", json={
            "provider": "stripe",
            "label": "Encryption Test",
            "credentials": {"api_key": plaintext_key},
        }, headers=headers)
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        # Read directly from DB
        result = await db.execute(
            select(Connection).where(Connection.id == uuid.UUID(conn_id))
        )
        conn = result.scalar_one_or_none()
        assert conn is not None
        # The encrypted_credentials should NOT contain the plaintext key
        assert plaintext_key not in conn.encrypted_credentials
        # It should be a Fernet-encrypted blob (starts with gAAAAA typically)
        assert len(conn.encrypted_credentials) > 50

    async def test_encryption_key_version_stored(self, client: AsyncClient, admin_user, db: AsyncSession):
        _, headers = admin_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "netsuite",
            "label": "Version Test",
            "credentials": {"token": "abc"},
        }, headers=headers)
        conn_id = resp.json()["id"]

        result = await db.execute(
            select(Connection).where(Connection.id == uuid.UUID(conn_id))
        )
        conn = result.scalar_one_or_none()
        assert conn is not None
        assert conn.encryption_key_version >= 1
