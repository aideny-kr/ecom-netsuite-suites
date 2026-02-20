"""NetSuite metadata discovery service.

Runs SuiteQL queries to discover custom fields, record types, and
organisational hierarchies, then stores results per-tenant and triggers
downstream prompt-template regeneration and RAG document seeding.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.models.netsuite_metadata import NetSuiteMetadata
from app.services.audit_service import log_event

logger = structlog.get_logger()

# ──────────────────────────────────────────────────────────────────
# Discovery query definitions
# ──────────────────────────────────────────────────────────────────

DISCOVERY_QUERIES: list[dict[str, Any]] = [
    {
        "label": "transaction_body_fields",
        "description": "Custom transaction body fields (custbody_*)",
        "query": (
            "SELECT scriptid, name, fieldtype, fieldvaluetype, ismandatory, lastmodifieddate "
            "FROM CustomField "
            "WHERE LOWER(scriptid) LIKE 'custbody%' AND ROWNUM <= 300"
        ),
    },
    {
        "label": "transaction_column_fields",
        "description": "Custom transaction line/column fields (custcol_*)",
        "query": (
            "SELECT scriptid, name, fieldtype, fieldvaluetype, lastmodifieddate "
            "FROM CustomField "
            "WHERE LOWER(scriptid) LIKE 'custcol%' AND ROWNUM <= 300"
        ),
    },
    {
        "label": "entity_custom_fields",
        "description": "Custom entity fields (custentity_*)",
        "query": (
            "SELECT scriptid, name, fieldtype, fieldvaluetype, ismandatory, lastmodifieddate "
            "FROM CustomField "
            "WHERE LOWER(scriptid) LIKE 'custentity%' AND ROWNUM <= 200"
        ),
    },
    {
        "label": "item_custom_fields",
        "description": "Custom item fields (custitem_*)",
        "query": (
            "SELECT scriptid, name, fieldtype, fieldvaluetype, lastmodifieddate "
            "FROM CustomField "
            "WHERE LOWER(scriptid) LIKE 'custitem%' AND ROWNUM <= 200"
        ),
    },
    {
        "label": "custom_record_types",
        "description": "Custom record type definitions",
        "query": ("SELECT scriptid, name, description FROM CustomRecordType WHERE ROWNUM <= 100"),
    },
    {
        "label": "custom_lists",
        "description": "Custom list definitions",
        "query": ("SELECT scriptid, name, description FROM CustomList WHERE ROWNUM <= 100"),
    },
    {
        "label": "subsidiaries",
        "description": "Subsidiary hierarchy",
        "query": ("SELECT id, name, isinactive, parent FROM subsidiary WHERE ROWNUM <= 100"),
    },
    {
        "label": "departments",
        "description": "Department hierarchy",
        "query": ("SELECT id, name, isinactive, parent FROM department WHERE ROWNUM <= 100"),
    },
    {
        "label": "classifications",
        "description": "Class/classification hierarchy",
        "query": ("SELECT id, name, isinactive, parent FROM classification WHERE ROWNUM <= 100"),
    },
    {
        "label": "locations",
        "description": "Location hierarchy",
        "query": ("SELECT id, name, isinactive, parent FROM location WHERE ROWNUM <= 100"),
    },
]


# ──────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────


async def _execute_discovery_query(
    access_token: str,
    account_id: str,
    query: str,
    label: str,
) -> dict:
    """Run a single discovery SuiteQL query and return normalised rows.

    Returns {"rows": [...], "columns": [...], "count": int}
    or {"error": str} on failure.
    """
    from app.services.netsuite_client import execute_suiteql

    try:
        result = await execute_suiteql(access_token, account_id, query)
        columns = result.get("columns", [])
        raw_rows = result.get("rows", [])
        # Convert to list-of-dicts for easier downstream consumption
        rows = [dict(zip(columns, row)) for row in raw_rows] if columns and raw_rows else []
        return {"rows": rows, "columns": columns, "count": len(rows)}
    except Exception as exc:
        logger.warning(
            "metadata.discovery_query_failed",
            label=label,
            error=str(exc),
        )
        return {"error": str(exc)}


def _count_fields(metadata: NetSuiteMetadata) -> int:
    """Count total fields across all discovery results."""
    total = 0
    for attr in (
        "transaction_body_fields",
        "transaction_column_fields",
        "entity_custom_fields",
        "item_custom_fields",
        "custom_record_types",
        "custom_lists",
        "subsidiaries",
        "departments",
        "classifications",
        "locations",
    ):
        val = getattr(metadata, attr, None)
        if isinstance(val, list):
            total += len(val)
    return total


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


async def run_full_discovery(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> NetSuiteMetadata:
    """Run all 10 discovery queries, persist results, and trigger downstream updates.

    Each individual query is wrapped in try/except so partial success is fine.
    """
    logger.info("metadata.discovery_started", tenant_id=str(tenant_id))

    # ── 1. Resolve NetSuite connection ──────────────────────────────
    conn_result = await db.execute(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
        .order_by((Connection.auth_type == "oauth2").desc(), Connection.created_at.desc())
        .limit(1)
    )
    connection = conn_result.scalar_one_or_none()
    if connection is None:
        raise ValueError("No active NetSuite connection found for this tenant.")

    credentials = decrypt_credentials(connection.encrypted_credentials)
    account_id = credentials["account_id"].replace("_", "-").lower()
    auth_type = credentials.get("auth_type", "oauth1")

    # For OAuth 2.0, refresh the token if needed
    if auth_type == "oauth2":
        from app.services.netsuite_oauth_service import get_valid_token

        access_token = await get_valid_token(db, connection)
        if not access_token:
            raise ValueError("OAuth 2.0 token expired and refresh failed. Re-authorize.")
    else:
        # OAuth 1.0 — we can't use the simple execute_suiteql helper directly.
        # Fall back to the MCP tool's execute path which handles OAuth 1.0.
        raise ValueError(
            "Metadata discovery currently requires OAuth 2.0. Please re-connect NetSuite with OAuth 2.0 PKCE."
        )

    # ── 2. Auto-increment version ──────────────────────────────────
    result = await db.execute(
        select(func.coalesce(func.max(NetSuiteMetadata.version), 0)).where(NetSuiteMetadata.tenant_id == tenant_id)
    )
    next_version = (result.scalar() or 0) + 1

    metadata = NetSuiteMetadata(
        tenant_id=tenant_id,
        version=next_version,
        status="pending",
        discovered_by=user_id,
    )
    db.add(metadata)
    await db.flush()

    # ── 3. Run each discovery query ─────────────────────────────────
    errors: dict[str, str] = {}
    success_count = 0

    for qdef in DISCOVERY_QUERIES:
        label = qdef["label"]
        result = await _execute_discovery_query(
            access_token=access_token,
            account_id=account_id,
            query=qdef["query"],
            label=label,
        )

        if "error" in result:
            errors[label] = result["error"]
            setattr(metadata, label, None)
        else:
            setattr(metadata, label, result["rows"])
            success_count += 1

    # ── 4. Finalise metadata record ─────────────────────────────────
    metadata.status = "completed" if success_count > 0 else "failed"
    metadata.discovered_at = datetime.now(timezone.utc)
    metadata.discovery_errors = errors or None
    metadata.query_count = success_count
    metadata.total_fields_discovered = _count_fields(metadata)
    await db.flush()

    logger.info(
        "metadata.discovery_completed",
        tenant_id=str(tenant_id),
        version=next_version,
        queries_succeeded=success_count,
        queries_failed=len(errors),
        total_fields=metadata.total_fields_discovered,
    )

    # ── 5. Trigger downstream updates ───────────────────────────────
    try:
        await _regenerate_prompt_template(db, tenant_id, metadata)
    except Exception:
        logger.warning("metadata.prompt_template_regen_failed", exc_info=True)

    try:
        from app.services.netsuite_metadata_rag import seed_metadata_docs

        await seed_metadata_docs(db, tenant_id, metadata)
    except Exception:
        logger.warning("metadata.rag_seeding_failed", exc_info=True)

    # ── 6. Audit log ────────────────────────────────────────────────
    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="netsuite",
        action="netsuite.metadata_discovery_completed",
        actor_id=user_id,
        resource_type="netsuite_metadata",
        resource_id=str(metadata.id),
        payload={
            "version": next_version,
            "queries_succeeded": success_count,
            "queries_failed": len(errors),
            "total_fields": metadata.total_fields_discovered,
        },
    )

    await db.commit()
    return metadata


async def _regenerate_prompt_template(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    metadata: NetSuiteMetadata,
) -> None:
    """Re-generate the system prompt template with the new metadata section."""
    from app.services.onboarding_service import get_active_profile
    from app.services.prompt_template_service import generate_and_save_template

    profile = await get_active_profile(db, tenant_id)
    if profile is None:
        logger.info("metadata.skip_template_regen_no_profile", tenant_id=str(tenant_id))
        return

    await generate_and_save_template(db, tenant_id, profile)
    logger.info("metadata.prompt_template_regenerated", tenant_id=str(tenant_id))


async def get_active_metadata(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> NetSuiteMetadata | None:
    """Return the latest completed metadata record for a tenant."""
    result = await db.execute(
        select(NetSuiteMetadata)
        .where(
            NetSuiteMetadata.tenant_id == tenant_id,
            NetSuiteMetadata.status == "completed",
        )
        .order_by(NetSuiteMetadata.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
