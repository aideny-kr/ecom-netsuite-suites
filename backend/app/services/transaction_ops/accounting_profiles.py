"""Current accounting treatments, separate from immutable reconciliation inputs.

Profiles configure candidate eligibility, never approve a posting. Native evidence
is still revalidated for every member at preparation and again before execution.
"""

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services.audit_service import log_event
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_review import SCOPE_FIELDS, scope_projection
from app.services.transaction_ops.netsuite_reader import _account
from app.services.transaction_ops.sales_credit_profile import SalesCreditProfile

NAMESPACE = "transaction_accounting_profiles"


def config_scope(config):
    return scope_projection({key: getattr(config, key) for key in SCOPE_FIELDS})


def _profile(value, scope):
    if value is None:
        return None
    profile = SalesCreditProfile.model_validate(value)
    if profile.account_id != scope["netsuite_account_id"] or profile.subsidiary_id != scope["subsidiary_id"]:
        raise ValueError("accounting_profile_scope_mismatch")
    return profile.model_dump(mode="json")


async def sales_credit_profile(db, tenant_id, config):
    """Select fresh columns: a cached Connection must not outlive revocation.

    An explicit null disables a legacy mapping profile. Malformed overrides
    cannot silently fall back to a formerly enabled treatment.
    """
    await set_tenant_context(db, str(tenant_id))
    row = (
        await db.execute(
            select(Connection.metadata_json, Connection.status).where(
                Connection.tenant_id == tenant_id,
                Connection.id == config.netsuite_connection_id,
                Connection.provider == "netsuite",
            )
        )
    ).one_or_none()
    if row is None or row.status not in ACTIVE_CONNECTION_STATUSES or not config.enabled:
        return None
    scope = config_scope(config)
    metadata = row.metadata_json or {}
    profiles = metadata.get(NAMESPACE, {})
    if not isinstance(profiles, dict):
        raise ValueError("accounting_profiles_invalid")
    key = state.business_digest(scope)
    if key not in profiles:
        return _profile((config.mapping_json or {}).get("sales_credit_profile"), scope)
    entry = profiles[key]
    if (
        not isinstance(entry, dict)
        or entry.get("schema_version") != 1
        or entry.get("scope") != scope
        or "sales_credit_profile" not in entry
        or _account(metadata.get("account_id", "")) != scope["netsuite_account_id"]
    ):
        raise ValueError("accounting_profile_scope_mismatch")
    return _profile(entry["sales_credit_profile"], scope)


async def configure_sales_credit_profile(db, tenant_id, config_id, value, *, actor):
    """Audited backend setting; active historical reviews keep their snapshots."""
    await set_tenant_context(db, str(tenant_id))
    await state._human(db, tenant_id, actor, "connections.manage")
    config = await state.get_config(db, tenant_id, config_id, lock=True)
    if not config.enabled:
        raise state.StateError("accounting_config_disabled", 409)
    scope = config_scope(config)
    try:
        profile = _profile(value, scope)
    except (ValueError, TypeError):
        raise state.StateError("accounting_profile_invalid", 422) from None
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.id == config.netsuite_connection_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    metadata = dict(connection.metadata_json or {}) if connection else {}
    try:
        bound_account = _account(metadata.get("account_id", ""))
    except ValueError:
        bound_account = None
    if not connection or bound_account != scope["netsuite_account_id"]:
        raise state.StateError("accounting_connection_scope_unavailable", 409)
    existing = metadata.get(NAMESPACE, {})
    if not isinstance(existing, dict):
        raise state.StateError("accounting_profiles_invalid", 409)
    profiles = dict(existing)
    key = state.business_digest(scope)
    before = profiles.get(key)
    after = {"schema_version": 1, "scope": scope, "sales_credit_profile": profile}
    profiles[key] = after
    connection.metadata_json = {**metadata, NAMESPACE: profiles}
    audit = await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action="accounting.profile.configured",
        actor_id=actor.id,
        resource_type="connection",
        resource_id=str(connection.id),
        payload={
            "config_id": str(config.id),
            "scope": scope,
            "before": before,
            "after": after,
            "legacy_profile": (config.mapping_json or {}).get("sales_credit_profile"),
            "financial_writes": 0,
            "financial_approval": None,
        },
    )
    await state._commit(db, tenant_id)
    return {"scope": scope, "sales_credit_profile": profile, "audit_id": str(audit.id), "financial_writes": 0}
