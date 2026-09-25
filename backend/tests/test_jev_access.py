"""Who may call Jev, with which key, in which mode.

Decided 2026-09-24: Jev is on by default in every deployment. A tenant's own TypeSafe key
(a ``typesafe`` connection) wins over the deployment's platform key; with no key anywhere
the current model path runs unchanged. The tenant chooses live (default), shadow or off;
``JEV_RECON_RESOLUTION_MODE`` caps every tenant, and ``off`` is the kill switch.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.services.typesafe.access import load_setting, resolve_access


async def _connect(db, tenant, *, api_key: str | None = None, mode: str | None = None, raw: str | None = None):
    credentials = {"api_key": api_key} if api_key else {}
    db.add(
        Connection(
            tenant_id=tenant.id,
            provider="typesafe",
            label="TypeSafe Jev",
            status="active",
            auth_type="api_key",
            encrypted_credentials=raw or encrypt_credentials(credentials),
            metadata_json={"mode": mode} if mode else {},
        )
    )
    await db.flush()


@pytest.fixture
def platform_key(monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "platform-key-1234")
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")


@pytest.mark.asyncio
async def test_on_by_default_with_the_platform_key(db, tenant_a, platform_key):
    access = await resolve_access(db, tenant_a.id)

    assert access is not None
    assert (access.api_key, access.mode, access.key_source) == ("platform-key-1234", "live", "platform")


@pytest.mark.asyncio
async def test_the_deployment_default_cap_is_live(monkeypatch):
    # The field default, not whatever the test environment set.
    from app.core.config import Settings

    assert Settings.model_fields["JEV_RECON_RESOLUTION_MODE"].default == "live"


@pytest.mark.asyncio
async def test_no_key_anywhere_means_off(db, tenant_a, monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "")

    assert await resolve_access(db, tenant_a.id) is None


@pytest.mark.asyncio
async def test_the_tenants_own_key_wins(db, tenant_a, platform_key):
    await _connect(db, tenant_a, api_key="tenant-key-9876")

    access = await resolve_access(db, tenant_a.id)

    assert (access.api_key, access.key_source) == ("tenant-key-9876", "tenant")


@pytest.mark.asyncio
async def test_a_tenant_key_works_without_a_platform_key(db, tenant_a, monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "")
    await _connect(db, tenant_a, api_key="tenant-key-9876")

    access = await resolve_access(db, tenant_a.id)

    assert (access.api_key, access.mode) == ("tenant-key-9876", "live")


@pytest.mark.asyncio
@pytest.mark.parametrize(("tenant_mode", "expected"), [("live", "live"), ("shadow", "shadow")])
async def test_the_tenant_chooses_the_mode(db, tenant_a, platform_key, tenant_mode, expected):
    await _connect(db, tenant_a, mode=tenant_mode)

    access = await resolve_access(db, tenant_a.id)

    assert (access.mode, access.key_source) == (expected, "platform")


@pytest.mark.asyncio
async def test_a_tenant_can_switch_jev_off_even_with_its_own_key(db, tenant_a, platform_key):
    await _connect(db, tenant_a, api_key="tenant-key-9876", mode="off")

    assert await resolve_access(db, tenant_a.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cap", "tenant_mode", "expected"),
    [("shadow", "live", "shadow"), ("shadow", "shadow", "shadow"), ("off", "live", None), ("live", "shadow", "shadow")],
)
async def test_the_deployment_setting_caps_every_tenant(db, tenant_a, monkeypatch, cap, tenant_mode, expected):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "platform-key-1234")
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", cap)
    await _connect(db, tenant_a, api_key="tenant-key-9876", mode=tenant_mode)

    access = await resolve_access(db, tenant_a.id)

    assert (access.mode if access else None) == expected


@pytest.mark.asyncio
async def test_an_unreadable_tenant_key_fails_closed_instead_of_using_the_platform_key(db, tenant_a, platform_key):
    # A tenant that brought its own key must not have its data sent under ours silently.
    await _connect(db, tenant_a, raw="not-a-fernet-token")

    assert await resolve_access(db, tenant_a.id) is None
    setting = await load_setting(db, tenant_a.id)
    assert (setting.key_source, setting.effective_mode, setting.problem) == ("tenant", "off", "unreadable_key")


@pytest.mark.asyncio
async def test_another_tenants_key_never_applies(db, tenant_a, tenant_b, monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "")
    await _connect(db, tenant_b, api_key="tenant-b-key")

    assert await resolve_access(db, tenant_a.id) is None


@pytest.mark.asyncio
async def test_the_setting_describes_what_will_happen_without_revealing_keys(db, tenant_a, platform_key):
    await _connect(db, tenant_a, api_key="tenant-key-9876", mode="shadow")

    setting = await load_setting(db, tenant_a.id)

    assert (setting.tenant_mode, setting.effective_mode, setting.deployment_cap) == ("shadow", "shadow", "live")
    assert (setting.key_source, setting.key_hint) == ("tenant", "9876")
    assert "tenant-key" not in repr(setting)


@pytest.mark.asyncio
async def test_the_platform_key_is_never_hinted(db, tenant_a, platform_key):
    setting = await load_setting(db, tenant_a.id)

    assert (setting.key_source, setting.key_hint, setting.effective_mode) == ("platform", None, "live")


@pytest.mark.parametrize("mode", ["off", "shadow", "live"])
async def test_transaction_workflow_honors_tenant_mode_independently_of_resolution(
    db, tenant_a, platform_key, monkeypatch, mode
):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    monkeypatch.setattr(settings, "JEV_TRANSACTION_OPS_MODE", "live")
    await _connect(db, tenant_a, api_key="tenant-key-9876", mode=mode)
    access = await resolve_access(db, tenant_a.id, workflow="transaction_ops")
    assert (access.mode if access else "off") == mode
    if access:
        assert access.api_key == "tenant-key-9876"
    assert (await load_setting(db, tenant_a.id)).effective_mode == ("off" if mode == "off" else "shadow")
