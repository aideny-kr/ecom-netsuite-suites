"""A NetSuite token re-auth keeps the settings stored on the connection it lands on.

On 2026-10-04 Framework re-authorized its NetSuite connection. The callback replaced
metadata_json with {account_id, auth_type, restlet_url}, which erased the accounting
profiles stored there, and every credit-memo approval card stopped. Those settings
describe the NetSuite account the row is bound to: a re-auth of the same account keeps
them, and a row that changes accounts keeps none of them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.v1 import netsuite_auth
from app.api.v1.oauth_state import encode_state
from app.models.connection import Connection
from tests.conftest import create_test_tenant, create_test_user

PROD = "1234567"
SANDBOX = "1234567_SB1"
RESTLET = "https://1234567.restlets.api.netsuite.com/app/site/hosting/restlet.nl?script=1&deploy=1"
SETTINGS = {
    "transaction_accounting_profiles": {"digest": {"schema_version": 1, "sales_credit_profile": {"item_id": "1471"}}},
    "transaction_native_accounting_profiles": {"digest": {"schema_version": 1}},
}


class _Redis:
    def __init__(self, stored: dict[str, str]):
        self.stored = stored

    async def get(self, key):
        return self.stored.get(key)

    async def delete(self, key):
        self.stored.pop(key, None)

    async def aclose(self):
        return None


async def _reauthorize(db, monkeypatch, tenant, user, account_id, *, restlet_url=""):
    state = uuid.uuid4().hex
    blob = encode_state(
        code_verifier="v",
        account_id=account_id,
        tenant_id=str(tenant.id),
        user_id=str(user.id),
        restlet_url=restlet_url,
        client_id="client",
    )
    redis = _Redis({f"netsuite_oauth:{state}": blob})

    async def fake_redis():
        return redis

    async def fake_exchange(account, code, verifier, client_id=None):
        return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    monkeypatch.setattr(netsuite_auth, "_get_redis", fake_redis)
    monkeypatch.setattr(netsuite_auth, "exchange_code", fake_exchange)
    response = await netsuite_auth.callback(state=state, db=db, code="code")
    assert response.status_code == 200
    return (
        (
            await db.execute(
                select(Connection).where(Connection.tenant_id == tenant.id, Connection.provider == "netsuite")
            )
        )
        .scalars()
        .all()
    )


async def _bound_row(db, tenant, account_id, *, status="error", extra=None):
    row = Connection(
        tenant_id=tenant.id,
        provider="netsuite",
        label=f"NetSuite {account_id}",
        status=status,
        auth_type="oauth2",
        encrypted_credentials="x",
        metadata_json={"account_id": account_id, "auth_type": "oauth2", "restlet_url": RESTLET, **(extra or {})},
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_reauthorizing_the_same_account_keeps_its_settings(db, monkeypatch):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    row = await _bound_row(db, tenant, PROD, extra=SETTINGS)

    rows = await _reauthorize(db, monkeypatch, tenant, user, PROD, restlet_url=RESTLET)

    assert [r.id for r in rows] == [row.id]
    assert row.status == "active"
    assert row.metadata_json == {"account_id": PROD, "auth_type": "oauth2", "restlet_url": RESTLET, **SETTINGS}


@pytest.mark.asyncio
async def test_a_reauth_without_a_restlet_url_keeps_the_stored_one(db, monkeypatch):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    row = await _bound_row(db, tenant, PROD, extra=SETTINGS)

    await _reauthorize(db, monkeypatch, tenant, user, PROD)

    assert row.metadata_json["restlet_url"] == RESTLET
    assert row.metadata_json["transaction_accounting_profiles"] == SETTINGS["transaction_accounting_profiles"]


@pytest.mark.asyncio
async def test_a_row_that_changes_accounts_keeps_none_of_the_old_accounts_settings(db, monkeypatch):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    row = await _bound_row(db, tenant, PROD, status="active", extra=SETTINGS)

    await _reauthorize(db, monkeypatch, tenant, user, SANDBOX, restlet_url=RESTLET)

    assert row.metadata_json == {"account_id": SANDBOX, "auth_type": "oauth2", "restlet_url": RESTLET}


@pytest.mark.asyncio
async def test_a_reauth_never_overwrites_the_fresh_identity_with_a_stored_one(db, monkeypatch):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    row = await _bound_row(db, tenant, PROD, extra={**SETTINGS, "auth_type": "legacy"})

    await _reauthorize(db, monkeypatch, tenant, user, PROD, restlet_url=RESTLET)

    assert row.metadata_json["auth_type"] == "oauth2"
    assert row.metadata_json["account_id"] == PROD
