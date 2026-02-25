"""Seed tenant_entity_mapping from discovered NetSuite metadata.

Populates the pg_trgm fuzzy lookup table so the TenantEntityResolver can
map natural-language entity names to NetSuite script IDs in sub-100ms.

Called automatically at the end of the metadata discovery pipeline
(both the async service path and the Celery worker path).

Uses ON CONFLICT upsert so a partial failure never leaves the tenant
with zero mappings — existing rows are updated in-place.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.netsuite_metadata import NetSuiteMetadata
from app.models.tenant_entity_mapping import TenantEntityMapping

logger = structlog.get_logger()

# Max length for VARCHAR(255) columns
_MAX_LEN = 255


def _truncate(value: str) -> str:
    return value[:_MAX_LEN] if len(value) > _MAX_LEN else value


def _build_rows(
    tenant_id: uuid.UUID,
    metadata: NetSuiteMetadata,
) -> list[dict]:
    """Extract all entity mappings from metadata into flat dicts."""
    rows: list[dict] = []

    # ── Custom record types ──────────────────────────────────────────
    if metadata.custom_record_types and isinstance(metadata.custom_record_types, list):
        for r in metadata.custom_record_types:
            scriptid = r.get("scriptid", "")
            name = r.get("name", "")
            if not scriptid or not name:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "customrecord",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": r.get("description") or None,
                }
            )

    # ── Transaction body fields ──────────────────────────────────────
    if metadata.transaction_body_fields and isinstance(metadata.transaction_body_fields, list):
        for f in metadata.transaction_body_fields:
            scriptid = f.get("scriptid", "")
            name = f.get("name", "")
            if not scriptid or not name:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "transactionbodyfield",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": f"Type: {f.get('fieldtype', '?')}",
                }
            )

    # ── Transaction column/line fields ───────────────────────────────
    if metadata.transaction_column_fields and isinstance(metadata.transaction_column_fields, list):
        for f in metadata.transaction_column_fields:
            scriptid = f.get("scriptid", "")
            name = f.get("name", "")
            if not scriptid or not name:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "transactioncolumnfield",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": f"Type: {f.get('fieldtype', '?')}",
                }
            )

    # ── Entity custom fields ────────────────────────────────────────
    if metadata.entity_custom_fields and isinstance(metadata.entity_custom_fields, list):
        for f in metadata.entity_custom_fields:
            scriptid = f.get("scriptid", "")
            name = f.get("name", "")
            if not scriptid or not name:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "entitycustomfield",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": f"Type: {f.get('fieldtype', '?')}",
                }
            )

    # ── Item custom fields ──────────────────────────────────────────
    if metadata.item_custom_fields and isinstance(metadata.item_custom_fields, list):
        for f in metadata.item_custom_fields:
            scriptid = f.get("scriptid", "")
            name = f.get("name", "")
            if not scriptid or not name:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "itemcustomfield",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": f"Type: {f.get('fieldtype', '?')}",
                }
            )

    # ── Custom record fields ────────────────────────────────────────
    if metadata.custom_record_fields and isinstance(metadata.custom_record_fields, list):
        for f in metadata.custom_record_fields:
            scriptid = f.get("scriptid", "")
            name = f.get("name", "")
            if not scriptid or not name:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "customrecordfield",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": f"Type: {f.get('fieldtype', '?')}",
                }
            )

    # ── Custom lists ────────────────────────────────────────────────
    #    Skip entries without valid customlist_* scriptids (some have display names)
    if metadata.custom_lists and isinstance(metadata.custom_lists, list):
        for cl in metadata.custom_lists:
            scriptid = cl.get("scriptid", "")
            name = cl.get("name", "")
            if not scriptid or not name:
                continue
            if not scriptid.lower().startswith("customlist"):
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "customlist",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": cl.get("description") or None,
                }
            )

    # ── Custom List Values (Dynamic) ────────────────────────────────
    # These represent the exact internal ID integer values for options
    # e.g., mapping "Failed" to internal ID '3' within customlist_integration_status
    if getattr(metadata, "custom_list_values", None) and isinstance(metadata.custom_list_values, dict):
        for table_name, list_values in metadata.custom_list_values.items():
            if not isinstance(list_values, list):
                continue
            for val in list_values:
                internal_id = str(val.get("id", ""))
                name = val.get("name", "")
                if not internal_id or not name:
                    continue
                rows.append(
                    {
                        "tenant_id": tenant_id,
                        "entity_type": "customlistvalue",
                        "natural_name": _truncate(name),
                        # We store the parent list script ID AND the value's internal ID
                        # so the AI knows exactly what context it belongs to
                        "script_id": _truncate(f"{table_name}.{internal_id}"),
                        "description": f"Value for list: {table_name}",
                    }
                )

    # ── Saved Searches ─────────────────────────────────────────────
    if getattr(metadata, "saved_searches", None) and isinstance(metadata.saved_searches, list):
        for ss in metadata.saved_searches:
            title = ss.get("title", "")
            ss_id = str(ss.get("id", ""))
            if not title or not ss_id:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "savedsearch",
                    "natural_name": _truncate(title),
                    "script_id": _truncate(ss_id),
                    "description": f"Record type: {ss.get('recordtype', 'unknown')}",
                }
            )

    # ── Scripts ────────────────────────────────────────────────────
    if getattr(metadata, "scripts", None) and isinstance(metadata.scripts, list):
        for s in metadata.scripts:
            scriptid = s.get("scriptid", "")
            name = s.get("name", "")
            if not scriptid or not name:
                continue
            desc = f"Type: {s.get('scripttype', 'unknown')}"
            if s.get("description"):
                desc += f" — {s['description']}"
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "script",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": desc,
                }
            )

    # ── Script Deployments ─────────────────────────────────────────
    if getattr(metadata, "script_deployments", None) and isinstance(metadata.script_deployments, list):
        for d in metadata.script_deployments:
            scriptid = d.get("scriptid", "")
            if not scriptid:
                continue
            name = d.get("title") or scriptid
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "scriptdeployment",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": (
                        f"Deployed on: {d.get('recordtype', 'unknown')}, "
                        f"Status: {d.get('status', 'unknown')}"
                    ),
                }
            )

    # ── Workflows ──────────────────────────────────────────────────
    if getattr(metadata, "workflows", None) and isinstance(metadata.workflows, list):
        for w in metadata.workflows:
            scriptid = w.get("scriptid", "")
            name = w.get("name", "")
            if not scriptid or not name:
                continue
            desc = f"Record type: {w.get('recordtype', 'unknown')}"
            if w.get("description"):
                desc += f" — {w['description']}"
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "entity_type": "workflow",
                    "natural_name": _truncate(name),
                    "script_id": _truncate(scriptid.lower()),
                    "description": desc,
                }
            )

    return rows


async def seed_entity_mappings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    metadata: NetSuiteMetadata,
) -> int:
    """Upsert entity mappings for a tenant from fresh metadata.

    Uses Postgres ON CONFLICT DO UPDATE so partial failures never
    leave the tenant with zero mappings. Stale rows (entities removed
    from NetSuite) are cleaned up after the upsert.

    Returns the number of mappings upserted.
    """
    rows = _build_rows(tenant_id, metadata)
    if not rows:
        return 0

    # Deduplicate: ON CONFLICT DO UPDATE cannot handle duplicate keys
    # within a single INSERT batch. Keep last occurrence (wins).
    seen: dict[tuple, int] = {}
    for idx, r in enumerate(rows):
        seen[(r["entity_type"], r["script_id"])] = idx
    rows = [rows[i] for i in sorted(seen.values())]

    # Batch upsert using Postgres INSERT ... ON CONFLICT DO UPDATE
    stmt = pg_insert(TenantEntityMapping).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_tenant_entity_type_script",
        set_={
            "natural_name": stmt.excluded.natural_name,
            "description": stmt.excluded.description,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await db.execute(stmt)

    # Clean up stale rows: delete mappings for this tenant that weren't
    # in the current metadata set (entity was removed from NetSuite)
    current_keys = {(r["entity_type"], r["script_id"]) for r in rows}
    result = await db.execute(
        text("SELECT id, entity_type, script_id FROM tenant_entity_mapping WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    stale_ids = [row.id for row in result if (row.entity_type, row.script_id) not in current_keys]
    if stale_ids:
        await db.execute(delete(TenantEntityMapping).where(TenantEntityMapping.id.in_(stale_ids)))
        logger.info(
            "entity_mapping.stale_removed",
            tenant_id=str(tenant_id),
            removed_count=len(stale_ids),
        )

    await db.flush()

    logger.info(
        "entity_mapping.seeded",
        tenant_id=str(tenant_id),
        upserted_count=len(rows),
        stale_removed=len(stale_ids) if stale_ids else 0,
    )
    return len(rows)
