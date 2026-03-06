"""Import tenant data from an export JSON file.

Uses INSERT ... ON CONFLICT DO UPDATE for idempotent imports.

Usage:
    cd backend && python -m scripts.import_tenant --input tenant_export_bf92d059_20260306.json
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from app.core.database import async_session_factory

# Import order matches FK dependencies (parents first)
IMPORT_ORDER = [
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

# Tables that use 'id' as primary key for ON CONFLICT
PK_COLUMN = "id"


async def import_tenant(input_path: str, dry_run: bool = False) -> dict:
    """Import tenant data from JSON export."""
    data = json.loads(Path(input_path).read_text())
    tenant_id = data["tenant_id"]
    results: dict[str, int] = {}

    print(f"Importing tenant {tenant_id} from {input_path}")
    if dry_run:
        print("  DRY RUN — no changes will be made")

    async with async_session_factory() as db:
        await db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": tenant_id})

        for table in IMPORT_ORDER:
            rows = data.get("tables", {}).get(table, [])
            if not rows:
                print(f"  {table}: 0 rows (skipped)")
                results[table] = 0
                continue

            # Skip rows with excluded credentials
            rows = [r for r in rows if r.get("encrypted_credentials") != "__EXCLUDED__"]

            if not rows:
                print(f"  {table}: 0 rows after filtering")
                results[table] = 0
                continue

            if dry_run:
                print(f"  {table}: {len(rows)} rows (would import)")
                results[table] = len(rows)
                continue

            # Build upsert for each row
            imported = 0
            for row in rows:
                columns = list(row.keys())
                col_names = ", ".join(columns)
                placeholders = ", ".join(f":{c}" for c in columns)
                update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != PK_COLUMN)

                sql = f"""
                    INSERT INTO {table} ({col_names})
                    VALUES ({placeholders})
                    ON CONFLICT ({PK_COLUMN}) DO UPDATE SET {update_set}
                """  # noqa: E501

                try:
                    await db.execute(text(sql), row)
                    imported += 1
                except Exception as exc:
                    print(f"    WARN: {table} row {row.get('id', '?')}: {exc}")

            results[table] = imported
            print(f"  {table}: {imported}/{len(rows)} rows imported")

        if not dry_run:
            await db.commit()
            print("\nCommitted successfully.")

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Import tenant data from export JSON")
    parser.add_argument("--input", required=True, help="Path to export JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()

    asyncio.run(import_tenant(args.input, args.dry_run))


if __name__ == "__main__":
    main()
