"""Tenant-specific native capabilities and accounting policy; never write approval.

The existing correction paths keep their contracts. New native amendments have
no default customer mappings and require an explicitly configured profile.
"""

from typing import Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.schemas.transaction_ops import EvidenceModel
from app.services.audit_service import log_event
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_profiles import config_scope
from app.services.transaction_ops.netsuite_reader import _account

NAMESPACE = "native_accounting_amendment_profiles"


class NativeFieldMap(EvidenceModel):
    order_reference: str = Field(pattern=r"^custbody_[a-z0-9_]{1,100}$")
    source_line_id: str = Field(pattern=r"^custcol_[a-z0-9_]{1,100}$")
    original_sku: str = Field(pattern=r"^custcol_[a-z0-9_]{1,100}$")
    vat_amount: str = Field(pattern=r"^custcol_[a-z0-9_]{1,100}$")

    @model_validator(mode="after")
    def distinct_fields(self):
        values = tuple(self.model_dump().values())
        if len(set(values)) != len(values):
            raise ValueError("native_accounting_fields_must_be_distinct")
        return self


class NativeAccountingProfile(EvidenceModel):
    schema_version: Literal[1]
    enabled: bool = False
    account_id: str
    subsidiary_id: str = Field(pattern=r"^[1-9][0-9]{0,29}$")
    role_id: str = Field(pattern=r"^[1-9][0-9]{0,29}$")
    accounting_book_id: str = Field(pattern=r"^[1-9][0-9]{0,29}$")
    tax_regime: Literal["legacy"]
    treatment: Literal["restore_existing_source_tax_allocation"]
    source_adapter: Literal["solidus"]
    fields: NativeFieldMap
    ar_account_ids: list[str] = Field(min_length=1, max_length=30)
    tax_account_ids: list[str] = Field(min_length=1, max_length=30)
    adjustment_account_ids: list[str] = Field(min_length=1, max_length=30)

    @field_validator("account_id")
    @classmethod
    def normalize_account(cls, value):
        return _account(value)

    @field_validator("ar_account_ids", "tax_account_ids", "adjustment_account_ids")
    @classmethod
    def account_ids(cls, value):
        import re

        if any(not re.fullmatch(r"[1-9][0-9]{0,29}", item) for item in value) or len(set(value)) != len(value):
            raise ValueError("verified_native_account_ids_required")
        return sorted(value)


def validate_profile(value, scope):
    if value is None:
        return None
    profile = NativeAccountingProfile.model_validate(value)
    if (profile.account_id, profile.subsidiary_id) != (scope["netsuite_account_id"], scope["subsidiary_id"]):
        raise ValueError("native_accounting_profile_scope_mismatch")
    return profile.model_dump(mode="json")


async def get_profile(db, tenant_id, config):
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
        raise ValueError("native_accounting_profiles_invalid")
    entry = profiles.get(state.business_digest(scope))
    if entry is None:
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("scope") != scope
        or _account(metadata.get("account_id", "")) != scope["netsuite_account_id"]
    ):
        raise ValueError("native_accounting_profile_scope_mismatch")
    profile = validate_profile(entry.get("profile"), scope)
    if not profile or not profile["enabled"]:
        return None
    revision = state.business_digest(profile)
    if entry.get("revision") != revision:
        raise ValueError("native_accounting_profile_revision_mismatch")
    return {**profile, "revision": revision}


async def configure_profile(db, tenant_id, config_id, value, *, actor):
    await set_tenant_context(db, str(tenant_id))
    await state._human(db, tenant_id, actor, "connections.manage")
    config = await state.get_config(db, tenant_id, config_id, lock=True)
    if not config.enabled:
        raise state.StateError("accounting_config_disabled", 409)
    scope = config_scope(config)
    try:
        profile = validate_profile(value, scope)
    except (ValueError, TypeError):
        raise state.StateError("native_accounting_profile_invalid", 422) from None
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.id == config.netsuite_connection_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    metadata = dict(connection.metadata_json or {}) if connection else {}
    try:
        bound_account = _account(metadata.get("account_id", ""))
    except ValueError:
        bound_account = None
    if not connection or bound_account != scope["netsuite_account_id"]:
        raise state.StateError("accounting_connection_scope_unavailable", 409)
    profiles = metadata.get(NAMESPACE, {})
    if not isinstance(profiles, dict):
        raise state.StateError("native_accounting_profiles_invalid", 409)
    key = state.business_digest(scope)
    before = profiles.get(key)
    after = {"scope": scope, "profile": profile, "revision": state.business_digest(profile)}
    connection.metadata_json = {**metadata, NAMESPACE: {**profiles, key: after}}
    event = await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting.native_profile.configured",
        actor_id=actor.id,
        resource_type="connection",
        resource_id=str(connection.id),
        payload={
            "config_id": str(config.id),
            "scope": scope,
            "before": before,
            "after": after,
            "financial_writes": 0,
            "financial_approval": None,
        },
    )
    await state._commit(db, tenant_id)
    return {**after, "audit_id": str(event.id), "financial_writes": 0}
