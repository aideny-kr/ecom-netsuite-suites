"""Actual PostgreSQL/API lifecycle; no live company policy or provider mutations."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models.audit import AuditEvent
from app.models.tenant import Tenant
from app.schemas.accounting_context import ContextDecision, ContextDraft, ContextScope
from app.services.transaction_ops import context_provenance as service
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_accounting_profiles import setup

SCOPE = ContextScope(accounting_book_id="1", currency="USD", posting_period_id="100")


def draft(version=0, **changes):
    now = datetime.now(timezone.utc)
    return ContextDraft.model_validate(
        {
            "expected_version": version,
            "key": "revenue",
            "topic": "revenue",
            "kind": "company_policy",
            "scope": SCOPE.model_dump(),
            "statement": "Synthetic reviewed policy example",
            "owner": "Test accounting owner",
            "sources": [
                {
                    "reference": "synthetic:policy-1",
                    "sha256": "a" * 64,
                    "observed_at": (now - timedelta(hours=1)).isoformat(),
                }
            ],
            "effective_from": (now - timedelta(days=1)).isoformat(),
            "review_by": (now + timedelta(days=1)).isoformat(),
            **changes,
        }
    )


def decision(view, kind="approve", index=0, **changes):
    return ContextDecision(
        expected_version=view["version"],
        content_sha256=view["entries"][index]["content_sha256"],
        decision=kind,
        reason="Synthetic human review",
        authority_reference="synthetic:approval-1",
        **changes,
    )


async def read(db, actor, config, scope=SCOPE):
    return await service.read_context(db, actor.tenant_id, config.id, actor=actor, scope=scope)


async def propose(db, actor, config, request=None):
    return await service.propose_context(db, actor.tenant_id, config.id, request or draft(), actor=actor)


async def approve(db, actor, config, key="revenue", view=None):
    return await service.decide_context(
        db, actor.tenant_id, config.id, key, decision(view or await read(db, actor, config)), actor=actor
    )


async def test_version_history_review_and_revalidation_preserve_existing_treatment(db, admin_user):
    actor = admin_user[0]
    config, connection, _ = await setup(db, actor, legacy=True)
    original_mapping = deepcopy(config.mapping_json)
    original_metadata = deepcopy(connection.metadata_json)
    assert (await read(db, actor, config))["version"] == 0
    await propose(db, actor, config)
    v1 = await read(db, actor, config)
    assert v1["entries"][0]["status"] == "review_required"
    assert not v1["entries"][0]["usable_as_policy"]
    await approve(db, actor, config, view=v1)
    v2 = await read(db, actor, config)
    assert v2["entries"][0]["usable_as_policy"] and v2["version"] == 2
    assert v2["entries"][0]["review"]["actor_id"] == str(actor.id)
    await propose(db, actor, config, draft(2, statement="Revised synthetic policy"))
    v3 = await read(db, actor, config)
    assert v3["entries"][0]["status"] == "review_required" and v3["entries"][0]["revision"] == 2
    assert v3["entries"][0]["review"] is None
    with pytest.raises(state.StateError, match="context_version_changed"):
        await approve(db, actor, config, view=v2)
    await approve(db, actor, config, view=v3)
    # Force a real changed fingerprint independent of the fixture's existing action mode.
    connection.auth_type = "oauth2_changed"
    await db.flush()
    v4 = await read(db, actor, config)
    assert v4["entries"][0]["status"] == "revalidation_required"
    with pytest.raises(state.StateError, match="context_new_revision_required"):
        await approve(db, actor, config, view=v4)
    await propose(db, actor, config, draft(4))
    await approve(db, actor, config)
    assert (await read(db, actor, config))["entries"][0]["usable_as_policy"]
    h = await service.context_history(db, actor.tenant_id, config.id, actor=actor, limit=2)
    assert [v["version"] for v in h["versions"]] == [6, 5] and h["next_before_version"] == 5
    h2 = await service.context_history(db, actor.tenant_id, config.id, actor=actor, before_version=5, limit=100)
    assert [v["version"] for v in h2["versions"]] == [4, 3, 2, 1]
    assert h2["versions"][-1]["entries"]["revenue"]["status"] == "review_required"
    assert h2["versions"][-2]["entries"]["revenue"]["status"] == "approved"
    assert connection.metadata_json == original_metadata
    assert config.mapping_json == original_mapping
    assert all(v["financial_writes"] == 0 and v["financial_approval"] is None for v in h2["versions"])


async def test_inference_cannot_be_approved_or_reclassified(db, admin_user):
    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config, draft(kind="inference"))
    v = await read(db, actor, config)
    with pytest.raises(state.StateError, match="inference_is_not_policy"):
        await approve(db, actor, config, view=v)
    with pytest.raises(state.StateError, match="context_kind_immutable"):
        await propose(db, actor, config, draft(1))
    assert (await read(db, actor, config))["version"] == 1
    assert not v["entries"][0]["usable_as_policy"]


@pytest.mark.parametrize(
    "field,value", [("accounting_book_id", "2"), ("currency", "EUR"), ("posting_period_id", "101")]
)
async def test_exact_book_currency_period_scope_no_fallback(db, admin_user, field, value):
    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config)
    await approve(db, actor, config)
    different = ContextScope.model_validate({**SCOPE.model_dump(), field: value})
    entry = (await read(db, actor, config, different))["entries"][0]
    assert not entry["usable_as_policy"] and entry["content"] is None
    unscoped = await read(db, actor, config, None)
    assert unscoped["scope_required"] and unscoped["entries"][0]["content"] is None


async def test_company_and_subsidiary_context_is_not_borrowed(db, admin_user, admin_user_b):
    actor = admin_user[0]
    other = admin_user_b[0]
    config, _, _ = await setup(db, actor)
    foreign, _, _ = await setup(db, other)
    await propose(db, actor, config)
    await approve(db, actor, config)
    assert (await read(db, other, foreign))["entries"] == []
    from tests.test_transaction_ops_state_db import seed_config

    second = await seed_config(db, actor.tenant_id, actor, subsidiary_id="9")
    assert (await read(db, actor, second))["entries"] == []
    with pytest.raises(state.StateError):
        await service.read_context(db, other.tenant_id, config.id, actor=other, scope=SCOPE)
    with pytest.raises(state.StateError):
        await service.context_manifest(db, other.tenant_id, config, scope=SCOPE)


async def test_conflict_requires_explicit_invalidation_then_new_review(db, admin_user):
    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config)
    await approve(db, actor, config)
    await propose(db, actor, config, draft(2, key="other", statement="Conflicting source"))
    v = await read(db, actor, config)
    with pytest.raises(state.StateError, match="context_conflicting_source"):
        await service.decide_context(db, actor.tenant_id, config.id, "other", decision(v, index=1), actor=actor)
    await service.decide_context(db, actor.tenant_id, config.id, "revenue", decision(v, "invalidate"), actor=actor)
    v = await read(db, actor, config)
    await service.decide_context(db, actor.tenant_id, config.id, "other", decision(v, index=1), actor=actor)
    v = await read(db, actor, config)
    assert [e["status"] for e in v["entries"]] == ["invalidated", "approved"]


async def test_stale_effective_and_invalidation_are_inspectable(db, admin_user):
    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config)
    await approve(db, actor, config)
    e = await service._latest(db, actor.tenant_id, config.id)
    binding = await service._binding(db, actor.tenant_id, config)
    stale = service._project(e, binding, SCOPE.model_dump(), now=datetime.now(timezone.utc) + timedelta(days=2))
    assert stale["entries"][0]["status"] == "stale" and not stale["entries"][0]["usable_as_policy"]
    future = service._project(e, binding, SCOPE.model_dump(), now=datetime.now(timezone.utc) - timedelta(days=2))
    assert future["entries"][0]["status"] == "not_yet_effective"
    v = await read(db, actor, config)
    await service.decide_context(db, actor.tenant_id, config.id, "revenue", decision(v, "invalidate"), actor=actor)
    with pytest.raises(state.StateError, match="context_new_revision_required"):
        await approve(db, actor, config)


@pytest.mark.parametrize(
    "variant",
    [
        "foreign_actor",
        "revoked_permission",
        "service_actor",
        "inactive_actor",
        "inactive_tenant",
        "revoked_connection",
        "wrong_account",
        "disabled_config",
    ],
)
async def test_write_boundary_rechecks_current_authority(db, admin_user, admin_user_b, variant):
    actor = admin_user[0]
    config, connection, _ = await setup(db, actor)
    await propose(db, actor, config)
    v = await read(db, actor, config)
    if variant == "foreign_actor":
        actor = admin_user_b[0]
    elif variant == "revoked_permission":
        await db.execute(text("DELETE FROM user_roles WHERE user_id=:id"), {"id": actor.id})
    elif variant == "service_actor":
        actor.actor_type = "service"
    elif variant == "inactive_actor":
        actor.is_active = False
    elif variant == "inactive_tenant":
        (await db.get(Tenant, actor.tenant_id)).is_active = False
    elif variant == "revoked_connection":
        connection.status = "revoked"
    elif variant == "wrong_account":
        connection.metadata_json = {"account_id": "999"}
    else:
        config.enabled = False
    await db.flush()
    with pytest.raises(state.StateError):
        await service.decide_context(db, config.tenant_id, config.id, "revenue", decision(v), actor=actor)
    latest = await service._latest(db, config.tenant_id, config.id)
    assert latest.payload["version"] == 1


async def test_observed_configuration_is_verified_not_policy(db, admin_user):
    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config, draft(kind="observed_configuration"))
    await approve(db, actor, config)
    e = (await read(db, actor, config))["entries"][0]
    assert e["status"] == "verified" and not e["usable_as_policy"]


async def test_http_lifecycle_permissions_history_and_strict_payload(
    client, db, admin_user, admin_user_b, readonly_user
):
    actor, headers = admin_user
    config, _, _ = await setup(db, actor)
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    base = f"/api/v1/transaction-ops/configs/{config.id}/accounting-context"
    response = await client.post(base, json=draft().model_dump(mode="json"), headers=headers)
    assert response.status_code == 200, response.text
    response = await client.post(base + "/resolve", json=SCOPE.model_dump(), headers=headers)
    assert response.status_code == 200, response.text
    v = response.json()
    bad = {**decision(v).model_dump(), "actor_id": str(uuid4())}
    assert (await client.post(base + "/revenue/review", json=bad, headers=headers)).status_code == 422
    approved = await client.post(base + "/revenue/review", json=decision(v).model_dump(), headers=headers)
    assert approved.status_code == 200, approved.text
    assert (
        await client.post(base + "/revenue/review", json=decision(v).model_dump(), headers=headers)
    ).status_code == 409
    history = await client.get(base + "/history?limit=1", headers=headers)
    assert history.status_code == 200 and history.json()["next_before_version"] == 2
    assert (await client.get(base + "/history", headers=admin_user_b[1])).status_code in {403, 404}
    assert (await client.post(base, json=draft(2).model_dump(mode="json"), headers=readonly_user[1])).status_code == 403
    assert (await client.post(base + "/resolve", json={"currency": "USD"}, headers=headers)).status_code == 422
    audit = await db.scalar(select(AuditEvent).where(AuditEvent.id == approved.json()["audit_id"]))
    assert audit.actor_id == actor.id


async def test_agent_policy_read_returns_exact_version_and_rejects_foreign_config(db, admin_user, admin_user_b):
    from app.core.encryption import encrypt_credentials
    from app.mcp.tools import netsuite_accounting_context as tool

    actor = admin_user[0]
    config, connection, _ = await setup(db, actor)
    connection.encrypted_credentials = encrypt_credentials({"account_id": config.netsuite_account_id})
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    await propose(db, actor, config)
    approved = await approve(db, actor, config)
    context = {"db": db, "tenant_id": actor.tenant_id, "actor_id": actor.id}
    params = {
        "section": "policies",
        "connection_id": str(connection.id),
        "expected_account_id": config.netsuite_account_id,
        "context_config_id": str(config.id),
        **SCOPE.model_dump(),
    }
    from app.mcp.server import mcp_server

    result = await mcp_server.call_tool(
        "netsuite.accounting_context", params, str(actor.tenant_id), str(actor.id), db=db
    )
    assert result["success"], result
    provenance = result["sections"]["policies"]["configured_treatments"][0]["context_provenance"]
    assert provenance["audit_id"] == approved["audit_id"] and provenance["version"] == 2
    assert provenance["entries"][0]["usable_as_policy"]
    assert provenance["company_scope"]["subsidiary_id"] == config.subsidiary_id
    params["currency"] = "EUR"
    result = await tool.execute(params, context)
    provenance = result["sections"]["policies"]["configured_treatments"][0]["context_provenance"]
    assert not provenance["entries"][0]["usable_as_policy"] and provenance["entries"][0]["content"] is None
    foreign, _, _ = await setup(db, admin_user_b[0])
    params["context_config_id"] = str(foreign.id)
    result = await tool.execute(params, context)
    assert not result["success"] and result["sections"]["policies"]["reason"] == "context_config_unavailable"
    del params["posting_period_id"]
    assert (await tool.execute(params, context))["error"] == "invalid_parameters"


async def test_changed_evidence_clears_review_and_content_digest_binds_decision(db, admin_user):
    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config)
    await approve(db, actor, config)
    old = await read(db, actor, config)
    changed = draft(2)
    changed.sources[0].sha256 = "b" * 64
    await propose(db, actor, config, changed)
    current = await read(db, actor, config)
    assert current["entries"][0]["status"] == "review_required"
    request = decision(old).model_copy(update={"expected_version": 3})
    with pytest.raises(state.StateError, match="context_content_changed"):
        await service.decide_context(db, actor.tenant_id, config.id, "revenue", request, actor=actor)


async def test_investigation_manifest_exposes_revision_without_selecting_a_book(db, admin_user):
    from app.services.transaction_ops.accounting_profiles import config_scope
    from app.services.transaction_ops.accounting_review import accounting_context

    actor = admin_user[0]
    config, _, _ = await setup(db, actor)
    await propose(db, actor, config)
    approved = await approve(db, actor, config)
    review = await accounting_context(db, actor.tenant_id, config_scope(config), actor_id=actor.id)
    receipt = review["context_provenance"]
    assert receipt["audit_id"] == approved["audit_id"] and receipt["version"] == 2
    assert receipt["scope_required"] and receipt["entries"][0]["content"] is None
    assert not receipt["entries"][0]["usable_as_policy"]


# The fixture creates/migrates/drops its own database and logs in as suite_runtime.
from tests.test_dedicated_runtime import installation  # noqa: E402,F401


async def test_real_runtime_concurrent_versions_and_audit_append_only(installation):  # noqa: F811
    import asyncio

    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.database import set_tenant_context
    from app.models.user import User

    engine = installation["engine"]
    company = installation["company"]
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await set_tenant_context(db, str(company))
        actor = await db.scalar(select(User).where(User.tenant_id == company))
        config, _, _ = await setup(db, actor)
        await db.commit()

    async def write_one(key):
        async with AsyncSession(engine, expire_on_commit=False) as db:
            try:
                return await propose(db, actor, config, draft(key=key))
            except state.StateError as exc:
                await db.rollback()
                return exc.code

    results = await asyncio.wait_for(asyncio.gather(write_one("first"), write_one("second")), timeout=10)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "context_version_changed" in results
    async with AsyncSession(engine, expire_on_commit=False) as db:
        current = await read(db, actor, config)
        assert current["version"] == 1 and len(current["entries"]) == 1
        key = current["entries"][0]["key"]
        await service.decide_context(db, company, config.id, key, decision(current), actor=actor)
        assert (await read(db, actor, config))["entries"][0]["usable_as_policy"]
        # The deployed role can append/read these records but cannot revise history.
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE audit_events SET payload='{}' WHERE action=:action"), {"action": service.ACTION}
            )
        await db.rollback()


async def test_readonly_agent_and_investigation_cannot_bypass_context_read_permission(db, admin_user, readonly_user):
    from app.core.encryption import encrypt_credentials
    from app.mcp.server import mcp_server
    from app.services.transaction_ops.accounting_profiles import config_scope
    from app.services.transaction_ops.accounting_review import accounting_context

    actor = admin_user[0]
    reader = readonly_user[0]
    config, connection, _ = await setup(db, actor)
    connection.encrypted_credentials = encrypt_credentials({"account_id": config.netsuite_account_id})
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    await propose(db, actor, config)
    await approve(db, actor, config)
    params = {
        "section": "policies",
        "connection_id": str(connection.id),
        "expected_account_id": config.netsuite_account_id,
        "context_config_id": str(config.id),
        **SCOPE.model_dump(),
    }
    result = await mcp_server.call_tool(
        "netsuite.accounting_context", params, str(reader.tenant_id), str(reader.id), db=db
    )
    assert result["success"]  # Existing configuration-reference access remains available.
    provenance = result["sections"]["policies"]["configured_treatments"][0]["context_provenance"]
    assert provenance["reason"] == "permission_denied" and provenance["entries"] == []
    for actor_id in (reader.id, None):
        review = await accounting_context(db, actor.tenant_id, config_scope(config), actor_id=actor_id)
        assert review["context_provenance"]["reason"] == "permission_denied"
        assert review["context_provenance"]["entries"] == []
    # A previously granted actor loses visibility immediately when the role is revoked.
    await db.execute(text("DELETE FROM user_roles WHERE user_id=:actor"), {"actor": actor.id})
    manifest = await service.context_manifest(db, actor.tenant_id, config, actor_id=actor.id, scope=SCOPE)
    assert manifest["reason"] == "permission_denied" and manifest["entries"] == []


def test_context_governance_parameters_match_advertised_tool():
    from app.mcp.governance import TOOL_CONFIGS, validate_params
    from app.mcp.registry import TOOL_REGISTRY

    name = "netsuite.accounting_context"
    assert set(TOOL_CONFIGS[name]["allowlisted_params"]) == set(TOOL_REGISTRY[name]["params_schema"])
    params = {"section": "policies", "context_config_id": str(uuid4()), **SCOPE.model_dump()}
    assert all(validate_params(name, params)[k] == v for k, v in params.items())
