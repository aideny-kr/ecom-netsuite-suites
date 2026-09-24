"""The Jev card: key and mode in Connections (decided 2026-09-24).

Jev is on by default with the deployment's key. A tenant can save its own TypeSafe key
(checked with TypeSafe before it is stored), return to the platform key, and choose live,
shadow or off. The key is never returned, logged or audited; only its last four characters
are shown, and only for the tenant's own key.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.encryption import decrypt_credentials
from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.services.typesafe.access import resolve_access

URL = "/api/v1/connector-status/jev"
TENANT_KEY = "ts-tenant-key-abcd1234"


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "ts-platform-key-9999")
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")


@pytest.fixture
def jev_accepts(monkeypatch):
    checked = []

    async def fake_check(api_key, **_):
        checked.append(api_key)
        return None

    monkeypatch.setattr("app.api.v1.connector_status.check_key", fake_check)
    return checked


@pytest.fixture
def jev_rejects(monkeypatch):
    async def fake_check(api_key, **_):
        return "http_401"

    monkeypatch.setattr("app.api.v1.connector_status.check_key", fake_check)


async def _row(db, tenant_id):
    return (
        await db.execute(select(Connection).where(Connection.tenant_id == tenant_id, Connection.provider == "typesafe"))
    ).scalar_one_or_none()


async def _audit(db, tenant_id):
    rows = (
        (await db.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.category == "jev")))
        .scalars()
        .all()
    )
    return rows


async def test_on_by_default_with_the_platform_key(client, admin_user):
    _, headers = admin_user
    r = await client.get(URL, headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "mode": "live",
        "effective_mode": "live",
        "deployment_cap": "live",
        "key_source": "platform",
        "key_hint": None,
        "problem": None,
    }


async def test_no_key_anywhere_shows_off(client, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "")
    _, headers = admin_user
    body = (await client.get(URL, headers=headers)).json()
    assert (body["key_source"], body["effective_mode"]) == ("none", "off")


async def test_saving_a_key_checks_it_with_typesafe_and_stores_it_encrypted(client, admin_user, db, jev_accepts):
    user, headers = admin_user
    r = await client.put(f"{URL}/key", headers=headers, json={"api_key": TENANT_KEY})

    assert r.status_code == 200
    assert jev_accepts == [TENANT_KEY]
    body = r.json()
    assert (body["key_source"], body["key_hint"], body["effective_mode"]) == ("tenant", "1234", "live")
    assert TENANT_KEY not in r.text
    row = await _row(db, user.tenant_id)
    assert TENANT_KEY not in row.encrypted_credentials
    assert decrypt_credentials(row.encrypted_credentials) == {"api_key": TENANT_KEY}
    access = await resolve_access(db, user.tenant_id)
    assert (access.api_key, access.key_source) == (TENANT_KEY, "tenant")
    events = await _audit(db, user.tenant_id)
    assert [e.action for e in events] == ["jev.key_saved"]
    assert TENANT_KEY not in str(events[0].payload)


async def test_a_key_typesafe_rejects_is_not_stored(client, admin_user, db, jev_rejects):
    user, headers = admin_user
    r = await client.put(f"{URL}/key", headers=headers, json={"api_key": TENANT_KEY})

    assert r.status_code == 400
    assert "rejected" in r.json()["detail"].lower()
    assert await _row(db, user.tenant_id) is None


async def test_removing_the_key_returns_to_the_platform_key_and_keeps_the_mode(client, admin_user, db, jev_accepts):
    user, headers = admin_user
    await client.put(f"{URL}/key", headers=headers, json={"api_key": TENANT_KEY})
    await client.put(f"{URL}/mode", headers=headers, json={"mode": "shadow"})

    r = await client.delete(f"{URL}/key", headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert (body["key_source"], body["key_hint"], body["mode"]) == ("platform", None, "shadow")
    access = await resolve_access(db, user.tenant_id)
    assert (access.api_key, access.mode) == ("ts-platform-key-9999", "shadow")
    assert [e.action for e in await _audit(db, user.tenant_id)] == ["jev.key_saved", "jev.mode_set", "jev.key_removed"]


async def test_removing_a_key_that_is_not_there_is_a_404(client, admin_user):
    _, headers = admin_user
    assert (await client.delete(f"{URL}/key", headers=headers)).status_code == 404


@pytest.mark.parametrize("mode", ["shadow", "off", "live"])
async def test_the_tenant_chooses_the_mode(client, admin_user, db, mode):
    user, headers = admin_user
    r = await client.put(f"{URL}/mode", headers=headers, json={"mode": mode})

    assert r.status_code == 200
    assert (r.json()["mode"], r.json()["effective_mode"]) == (mode, mode)
    access = await resolve_access(db, user.tenant_id)
    assert (access.mode if access else "off") == mode
    event = (await _audit(db, user.tenant_id))[-1]
    assert (event.action, event.payload) == ("jev.mode_set", {"mode": mode})


async def test_an_unknown_mode_is_refused(client, admin_user):
    _, headers = admin_user
    assert (await client.put(f"{URL}/mode", headers=headers, json={"mode": "turbo"})).status_code == 422


async def test_the_deployment_cap_is_shown(client, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    _, headers = admin_user
    body = (await client.get(URL, headers=headers)).json()
    assert (body["mode"], body["deployment_cap"], body["effective_mode"]) == ("live", "shadow", "shadow")


async def test_test_checks_a_new_key_or_the_one_in_use(client, admin_user, jev_accepts):
    _, headers = admin_user
    r = await client.post(f"{URL}/test", headers=headers, json={"api_key": "ts-candidate-key"})
    assert r.json() == {"success": True, "key_source": "candidate", "error": None}

    r = await client.post(f"{URL}/test", headers=headers, json={})
    assert r.json() == {"success": True, "key_source": "platform", "error": None}
    assert jev_accepts == ["ts-candidate-key", "ts-platform-key-9999"]


async def test_test_reports_a_rejected_key(client, admin_user, jev_rejects):
    _, headers = admin_user
    body = (await client.post(f"{URL}/test", headers=headers, json={"api_key": "ts-bad"})).json()
    assert body["success"] is False and "rejected" in body["error"].lower()


async def test_test_with_no_key_anywhere_says_so(client, admin_user, monkeypatch, jev_accepts):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "")
    _, headers = admin_user
    body = (await client.post(f"{URL}/test", headers=headers, json={})).json()
    assert body == {"success": False, "key_source": "none", "error": "No Jev key is configured."}
    assert jev_accepts == []


async def test_a_readonly_user_can_see_but_not_change(client, readonly_user, jev_accepts):
    _, headers = readonly_user
    assert (await client.get(URL, headers=headers)).status_code == 200
    assert (await client.put(f"{URL}/key", headers=headers, json={"api_key": TENANT_KEY})).status_code == 403
    assert (await client.put(f"{URL}/mode", headers=headers, json={"mode": "off"})).status_code == 403
    assert (await client.delete(f"{URL}/key", headers=headers)).status_code == 403
    assert (await client.post(f"{URL}/test", headers=headers, json={})).status_code == 403
    assert jev_accepts == []


async def test_one_tenants_key_is_invisible_to_another(client, admin_user, admin_user_b, jev_accepts):
    _, headers_a = admin_user
    _, headers_b = admin_user_b
    await client.put(f"{URL}/key", headers=headers_a, json={"api_key": TENANT_KEY})

    body_b = (await client.get(URL, headers=headers_b)).json()
    assert (body_b["key_source"], body_b["key_hint"]) == ("platform", None)


# ── codex review of #314 ───────────────────────────────────────────────────


async def test_the_generic_connection_routes_cannot_see_or_change_the_jev_connection(
    client, admin_user, db, jev_accepts
):
    """The Jev row is managed only by its card. The generic connection service does not
    serve it, so no generic route (today's or a future one) can read or change it."""
    user, headers = admin_user
    await client.put(f"{URL}/key", headers=headers, json={"api_key": TENANT_KEY})
    row = await _row(db, user.tenant_id)
    before = (row.encrypted_credentials, dict(row.metadata_json or {}), row.label, row.status)

    listed = (await client.get("/api/v1/connections", headers=headers)).json()
    assert all(c["provider"] != "typesafe" for c in listed)
    base = f"/api/v1/connections/{row.id}"
    responses = [
        await client.delete(base, headers=headers),
        await client.patch(base, headers=headers, json={"label": "x"}),
        await client.post(f"{base}/reconnect", headers=headers),
        await client.patch(f"{base}/client-id", headers=headers, json={"client_id": "abc"}),
        await client.patch(f"{base}/restlet-url", headers=headers, json={"restlet_url": "https://x.example/r"}),
    ]
    assert [r.status_code for r in responses] == [404] * 5
    # The generic test route answers any unknown id with 200 + "Connection not found".
    tested = await client.post(f"{base}/test", headers=headers)
    assert (tested.json()["status"], tested.json()["message"]) == ("error", "Connection not found")

    await db.refresh(row)
    assert (row.encrypted_credentials, dict(row.metadata_json or {}), row.label, row.status) == before
    access = await resolve_access(db, user.tenant_id)
    assert (access.api_key, access.key_source) == (TENANT_KEY, "tenant")


async def test_a_short_stored_key_is_never_shown_as_its_own_hint(client, admin_user, db, tenant_a):
    from tests.test_jev_access import _connect

    _, headers = admin_user
    await _connect(db, tenant_a, api_key="abc")
    body = (await client.get(URL, headers=headers)).json()
    assert (body["key_source"], body["key_hint"]) == ("tenant", None)


async def test_testing_an_unreadable_stored_key_says_so(client, admin_user, db, tenant_a, jev_accepts):
    from tests.test_jev_access import _connect

    _, headers = admin_user
    await _connect(db, tenant_a, raw="not-a-fernet-token")
    body = (await client.post(f"{URL}/test", headers=headers, json={})).json()
    assert body["success"] is False and "could not be read" in body["error"]
    assert jev_accepts == []


@pytest.mark.parametrize("payload", [{"api_key": ["ts-secret-in-a-list"]}, {"api_key": {"k": "ts-secret-in-a-dict"}}])
async def test_a_malformed_key_is_refused_without_echoing_it(client, admin_user, jev_accepts, payload):
    _, headers = admin_user
    for method, path in (("put", f"{URL}/key"), ("post", f"{URL}/test")):
        r = await getattr(client, method)(path, headers=headers, json=payload)
        assert r.status_code == 400, (path, r.status_code)
        assert "ts-secret-in-a" not in r.text
    assert jev_accepts == []


async def test_an_over_long_key_is_refused_without_echoing_it(client, admin_user, db, jev_accepts):
    user, headers = admin_user
    long_key = "ts-" + "x" * 600

    r = await client.put(f"{URL}/key", headers=headers, json={"api_key": long_key})

    assert r.status_code == 400
    assert long_key not in r.text and "x" * 50 not in r.text
    assert jev_accepts == [] and await _row(db, user.tenant_id) is None


async def test_a_user_without_view_permission_cannot_see_jev(client, db, tenant_a):
    from tests.conftest import create_test_user, make_auth_headers

    user, _ = await create_test_user(db, tenant_a, role_name="no-such-role")
    r = await client.get(URL, headers=make_auth_headers(user))
    assert r.status_code == 403
