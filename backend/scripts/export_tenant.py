"""Export a single tenant's configuration and data for cross-environment migration.

Usage:
    cd backend && python -m scripts.export_tenant --tenant-id bf92d059-...
    cd backend && python -m scripts.export_tenant --tenant-id bf92d059-... --exclude-credentials
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.core.database import async_session_factory

# Tables to export in FK-safe order
EXPORT_TABLES = [
    "tenants",
    "tenant_configs",
    "users",
    "connections",
    "mcp_connectors",
    "tenant_feature_flags",
    "tenant_entity_mapping",
    "tenant_learned_rules",
    "tenant_query_patterns",
    "tenant_wallets",
    "saved_suiteql_queries",
]

# Tables excluded from export entirely
EXCLUDE_TABLES = {"audit_events", "netsuite_api_logs", "chat_sessions", "chat_messages"}


def _serialize(obj):
    """JSON serializer for non-standard types."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


async def export_tenant(
    tenant_id: uuid.UUID,
    exclude_credentials: bool = False,
) -> dict:
    """Export tenant data as a JSON-serializable dict."""
    export: dict = {
        "exported_at": datetime.utcnow().isoformat(),
        "tenant_id": str(tenant_id),
        "tables": {},
    }

    async with async_session_factory() as db:
        # Bypass RLS for export
        await db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_id)})

        for table in EXPORT_TABLES:
            try:
                if table == "tenants":
                    result = await db.execute(
                        text("SELECT * FROM tenants WHERE id = :tid"),
                        {"tid": tenant_id},
                    )
                else:
                    result = await db.execute(
                        text(f"SELECT * FROM {table} WHERE tenant_id = :tid"),  # noqa: E501
                        {"tid": tenant_id},
                    )

                rows = [dict(row._mapping) for row in result.fetchall()]

                # Serialize non-JSON-native types
                for row in rows:
                    for k, v in list(row.items()):
                        if isinstance(v, (uuid.UUID, datetime, bytes)):
                            row[k] = _serialize(v)

                # Strip credentials if requested
                if exclude_credentials:
                    if table == "connections":
                        for row in rows:
                            row["encrypted_credentials"] = "__EXCLUDED__"
                    if table == "tenant_configs":
                        for row in rows:
                            row["ai_api_key_encrypted"] = None
                    if table == "mcp_connectors":
                        for row in rows:
                            if "encrypted_credentials" in row:
                                row["encrypted_credentials"] = "__EXCLUDED__"

                export["tables"][table] = rows
                print(f"  {table}: {len(rows)} rows")

            except Exception as exc:
                print(f"  {table}: SKIPPED ({exc})")
                export["tables"][table] = []

    return export


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Export tenant data for migration")
    parser.add_argument("--tenant-id", required=True, help="UUID of the tenant")
    parser.add_argument("--exclude-credentials", action="store_true", help="Strip encrypted credentials")
    parser.add_argument("--output", default=None, help="Output file path")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant_id)
    data = asyncio.run(export_tenant(tid, args.exclude_credentials))

    out_path = args.output or f"tenant_export_{args.tenant_id[:8]}_{datetime.now().strftime('%Y%m%d')}.json"
    Path(out_path).write_text(json.dumps(data, indent=2, default=_serialize))
    print(f"\nExported to {out_path}")


if __name__ == "__main__":
    main()
