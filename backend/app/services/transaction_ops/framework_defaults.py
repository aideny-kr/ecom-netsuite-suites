"""Backend defaults grounded in Framework's verified sales-order mappings.

Verified 2026-09-07: imports66738c3d9711fdc90cd89e46/48 map number→tranid;
the international subsidiary_map and Inc branch provide the entity routing below.
These mappings apply only to the selected Framework account, never another tenant's
arbitrary NetSuite account. Existing operator configuration and pauses take priority.
"""

from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.celigo import CeligoFlowStep
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.transaction_ops import TransactionConfig
from app.schemas.transaction_runs import ConfigCreate
from app.services.http_connector_service import validate_credentials
from app.services.transaction_ops import state_service
from app.services.transaction_ops.netsuite_reader import _account
from app.services.transaction_ops.source_reader import _FRAMEWORK_BASE

FRAMEWORK_ACCOUNT = "6738075"
FRAMEWORK_REFUND_STEP_ID = UUID("223baf64-490d-4e5f-a1ef-08419d556163")
ENTITY_SUBSIDIARIES = {"Framework Inc": "1", "Framework BV": "2", "Framework AU": "4", "Framework UK": "5"}
_MINOR_UNITS = {"USD": 2, "CAD": 2, "EUR": 2, "GBP": 2, "AUD": 2, "JPY": 0, "TWD": 2, "CHF": 2, "SGD": 2, "NZD": 2}


async def refund_source_id(db, tenant_id):
    # This verified binding only applies when that saved database source belongs
    # to this tenant. Its SQL/hooks are never executed by the refund collector.
    identifier = await db.scalar(
        select(CeligoFlowStep.id)
        .join(Connection, Connection.id == CeligoFlowStep.celigo_connection_id)
        .where(
            CeligoFlowStep.id == FRAMEWORK_REFUND_STEP_ID,
            CeligoFlowStep.tenant_id == tenant_id,
            CeligoFlowStep.role == "generator",
            CeligoFlowStep.adaptor_type == "RDBMSExport",
            Connection.tenant_id == tenant_id,
            Connection.provider == "celigo",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
    )
    return str(identifier) if identifier else None


async def ensure_framework_configs(db, tenant_id, source_connection_id, *, actor):
    await set_tenant_context(db, tenant_id)
    await state_service._human(db, tenant_id, actor, "connections.manage")
    source = await db.scalar(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.id == source_connection_id,
            Connection.provider == "solidus",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
    )
    if source is None:
        raise state_service.StateError("source_unavailable", 422)
    try:
        credentials = validate_credentials("solidus", decrypt_credentials(source.encrypted_credentials))
        if credentials.get("api_profile") != "framework_sync" or credentials["base_url"] != _FRAMEWORK_BASE:
            raise ValueError
    except (InvalidToken, ValueError, KeyError, TypeError, AttributeError):
        raise state_service.StateError("source_unavailable", 422) from None
    targets = []
    for connection in (
        await db.scalars(
            select(Connection).where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            )
        )
    ).all():
        try:
            if _account(decrypt_credentials(connection.encrypted_credentials).get("account_id")) == FRAMEWORK_ACCOUNT:
                targets.append(connection)
        except (InvalidToken, ValueError, TypeError, AttributeError):
            continue
    if len(targets) != 1:
        raise state_service.StateError("framework_destination_unavailable", 422)
    target = targets[0]
    refund_step = await refund_source_id(db, tenant_id)
    existing = (
        await db.scalars(
            select(TransactionConfig)
            .where(
                TransactionConfig.tenant_id == tenant_id,
                TransactionConfig.source_connection_id == source.id,
                TransactionConfig.netsuite_connection_id == target.id,
                state_service.current_config_clause(),
            )
            .order_by(TransactionConfig.created_at)
        )
    ).all()
    result = []
    for entity, subsidiary in ENTITY_SUBSIDIARIES.items():
        configured = [row for row in existing if row.subsidiary_id == subsidiary]
        if len(configured) > 1:
            raise state_service.StateError("configured_scope_ambiguous", 409)
        if configured:
            result.append(configured[0])
            continue
        request = ConfigCreate(
            name=f"{entity} → NetSuite",
            source_connection_id=source.id,
            netsuite_connection_id=target.id,
            netsuite_account_id=FRAMEWORK_ACCOUNT,
            subsidiary_id=subsidiary,
            mapping_json={
                "reference_field": "tranid",
                "business_entity_subsidiaries": {entity: subsidiary},
                "currency_minor_units": _MINOR_UNITS,
                "action_mode": "propose_actions",
                "solidus_refund_step_id": refund_step,
            },
            schedule_enabled=True,
            interval_minutes=1440,
            max_api_calls=2000,
            max_orders=1000,
            deadline_seconds=900,
        )
        result.append(await state_service.create_config(db, tenant_id, request, actor=actor))
    return result
