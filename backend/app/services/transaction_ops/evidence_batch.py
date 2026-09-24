"""Scoped immutable batch storage; a batch is not a daily coverage certificate.

Only the worker supplies bodies. Authorization/credentials, config, phase and
scan floor fence reuse. Neither storage nor a hit advances observation times.
Write preflights never use this module.
"""

import copy
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.tenant import Tenant
from app.models.transaction_evidence_batch import TransactionEvidenceBatch as Batch
from app.models.transaction_ops import TransactionRun
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError, _account

VERSION = 1
MAX_BYTES = 4_000_000
MAX_AGE = timedelta(hours=24)


def _encode(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("invalid_batch_value")


def context_hash(config, phase, parent=None):
    return hashlib.sha256(
        json.dumps([VERSION, config, phase, parent], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def fingerprint(db, tenant_id, connection_id, account_id):
    await set_tenant_context(db, tenant_id)
    connection = await db.scalar(
        select(Connection)
        .join(Tenant, Tenant.id == Connection.tenant_id)
        .where(
            Tenant.id == tenant_id,
            Tenant.is_active.is_(True),
            Connection.tenant_id == tenant_id,
            Connection.id == UUID(str(connection_id)),
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise NetSuiteEvidenceError("invalid_connection")
    if _account(decrypt_credentials(connection.encrypted_credentials).get("account_id")) != _account(account_id):
        raise NetSuiteEvidenceError("account_mismatch")
    return hashlib.sha256(connection.encrypted_credentials.encode()).hexdigest()


async def save(db, tenant_id, run_id, kind, context, config, data, *, started_at, now):
    current = await fingerprint(db, tenant_id, config["netsuite_connection_id"], config["netsuite_account_id"])
    if data.get("credential_fingerprint") != current:
        raise NetSuiteEvidenceError("batch_credentials_changed")
    if kind not in {"orders", "refunds"} or not started_at <= now or now - started_at > MAX_AGE:
        raise NetSuiteEvidenceError("invalid_batch_scope")
    # Composite FK also enforces this at storage. Check it explicitly before any
    # write so a privileged worker cannot attach another tenant's run.
    if not await db.scalar(
        select(TransactionRun.id).where(TransactionRun.tenant_id == tenant_id, TransactionRun.id == run_id)
    ):
        raise NetSuiteEvidenceError("invalid_batch_run")
    payload = json.dumps({"version": VERSION, "data": data[kind]}, default=_encode, allow_nan=False, sort_keys=True)
    if len(payload.encode()) > MAX_BYTES:
        raise NetSuiteEvidenceError("batch_size_budget")
    identifier = uuid5(NAMESPACE_URL, json.dumps([str(tenant_id), str(run_id), kind, context, current, payload]))
    await db.execute(
        insert(Batch)
        .values(
            id=identifier,
            tenant_id=tenant_id,
            run_id=run_id,
            kind=kind,
            context_hash=context,
            connection_fingerprint=current,
            started_at=started_at,
            completed_at=now,
            evidence_json=json.loads(payload),
        )
        .on_conflict_do_nothing(index_elements=[Batch.id])
    )
    await db.commit()
    return str(identifier)


async def load(db, tenant_id, identifier, kind, context, config, *, since, now):
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError):
        return None
    current = await fingerprint(db, tenant_id, config["netsuite_connection_id"], config["netsuite_account_id"])
    row = await db.scalar(
        select(Batch).where(
            Batch.id == identifier,
            Batch.tenant_id == tenant_id,
            Batch.kind == kind,
            Batch.context_hash == context,
            Batch.connection_fingerprint == current,
            Batch.started_at >= max(since, now - MAX_AGE),
            Batch.completed_at <= now,
        )
    )
    if row is None or row.evidence_json.get("version") != VERSION:
        return None
    return copy.deepcopy(row.evidence_json["data"])
