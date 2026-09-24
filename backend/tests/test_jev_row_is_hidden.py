"""The TypeSafe Jev connection row is invisible to every ORM query that does not opt in.

Three review rounds on #314 each found one more generic route that could read or change
the Jev row (DELETE, then the PATCH routes, then GET /connections/health). Guarding routes
one at a time left the next one open, so the rule now lives at the ORM: every SELECT
on Connection excludes provider "typesafe" unless the query carries
``include_jev_connection=True``, which only typesafe.access and the Jev card use.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.connection import INCLUDE_JEV_CONNECTION, Connection
from app.models.tenant import Tenant
from app.services.typesafe.access import resolve_access
from tests.test_jev_access import _connect


async def _other_connection(db, tenant):
    from app.core.encryption import encrypt_credentials

    row = Connection(
        tenant_id=tenant.id,
        provider="stripe",
        label="Stripe",
        status="active",
        auth_type="api_key",
        encrypted_credentials=encrypt_credentials({"api_key": "sk_test"}),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture(autouse=True)
def _platform_key(monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "platform-key")


async def test_a_plain_query_never_returns_the_jev_row(db, tenant_a):
    await _connect(db, tenant_a, api_key="tenant-key-1234567890")
    await _other_connection(db, tenant_a)

    rows = (await db.execute(select(Connection).where(Connection.tenant_id == tenant_a.id))).scalars().all()
    assert [r.provider for r in rows] == ["stripe"]

    opted_in = (
        (
            await db.execute(
                select(Connection)
                .where(Connection.tenant_id == tenant_a.id)
                .execution_options(**{INCLUDE_JEV_CONNECTION: True})
            )
        )
        .scalars()
        .all()
    )
    assert sorted(r.provider for r in opted_in) == ["stripe", "typesafe"]


async def test_the_tenants_connections_relationship_hides_it(db, tenant_a):
    await _connect(db, tenant_a, api_key="tenant-key-1234567890")
    await _other_connection(db, tenant_a)

    tenant = (
        await db.execute(
            select(Tenant)
            .where(Tenant.id == tenant_a.id)
            .options(selectinload(Tenant.connections))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert [c.provider for c in tenant.connections] == ["stripe"]


async def test_the_resolver_and_a_refresh_still_see_it(db, tenant_a):
    await _connect(db, tenant_a, api_key="tenant-key-1234567890", mode="shadow")

    access = await resolve_access(db, tenant_a.id)
    assert (access.api_key, access.mode) == ("tenant-key-1234567890", "shadow")

    row = (
        await db.execute(
            select(Connection)
            .where(Connection.tenant_id == tenant_a.id, Connection.provider == "typesafe")
            .execution_options(**{INCLUDE_JEV_CONNECTION: True})
        )
    ).scalar_one()
    await db.refresh(row)  # an object already loaded keeps working
    assert row.provider == "typesafe"


async def test_connection_health_does_not_list_it(client, admin_user, db, tenant_a):
    await _connect(db, tenant_a, api_key="tenant-key-1234567890")
    _, headers = admin_user

    body = (await client.get("/api/v1/connections/health", headers=headers)).json()

    assert all(item.get("provider") != "typesafe" for item in body.get("connections", []))
    assert "TypeSafe Jev" not in str(body)
