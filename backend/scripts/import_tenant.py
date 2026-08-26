"""Import tenant data from an export JSON file.

Uses INSERT ... ON CONFLICT DO UPDATE for idempotent imports.

Usage:
    cd backend && python -m scripts.import_tenant --input tenant_export_bf92d059_20260306.json

CELIGO ROWS ARE SKIPPED BY DEFAULT. This script writes below the ORM with
generic textual SQL, so the session-flush guard in
``app/services/celigo_write_guard.py`` -- which refuses Celigo writes coming
from anywhere but the dedicated connect/disconnect flow -- cannot see it. That
guard exists because the two Celigo rows (the ``connections`` row and its
paired ``celigo_mcp`` ``mcp_connectors`` row) only make sense together, and an
import that lands one without the other, or lands a token that was never
verified against Celigo, produces exactly the incoherence the guard was built
to make unrepresentable. Rather than route this script through the guard (which
would hand a blanket opt-out to a tool whose whole job is bulk generic writes),
it carries its own refusal: pass ``--allow-celigo`` to import them anyway, and
re-verify the connection in Settings -> Integrations afterwards.
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

# provider values this script refuses to write without --allow-celigo.
# Mirrors GUARDED_TABLES in app/services/celigo_write_guard.py; kept as a
# literal here rather than imported because this script deliberately does NOT
# go through the guard (see module docstring).
CELIGO_PROVIDERS = {"celigo", "celigo_mcp"}


def _drop_celigo_rows(table: str, rows: list[dict]) -> tuple[list[dict], int]:
    """Filter Celigo rows out of *rows*, returning (kept, dropped_count)."""
    kept = [r for r in rows if r.get("provider") not in CELIGO_PROVIDERS]
    return kept, len(rows) - len(kept)


async def import_tenant(input_path: str, dry_run: bool = False, allow_celigo: bool = False) -> dict:
    """Import tenant data from JSON export.

    *allow_celigo* opts in to importing ``celigo``/``celigo_mcp`` rows, which
    are skipped by default -- see the module docstring for why.
    """
    data = json.loads(Path(input_path).read_text())
    tenant_id = data["tenant_id"]
    results: dict[str, int] = {}

    print(f"Importing tenant {tenant_id} from {input_path}")
    if dry_run:
        print("  DRY RUN — no changes will be made")
    if allow_celigo:
        print("  --allow-celigo: Celigo rows WILL be written below the ORM guard.")
        print("  Re-verify the connection in Settings -> Integrations after this import.")

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

            # Refuse Celigo rows unless explicitly opted in. Filtered here, before
            # the dry-run branch, so --dry-run reports the counts the real run
            # would actually write.
            if not allow_celigo:
                rows, dropped = _drop_celigo_rows(table, rows)
                if dropped:
                    print(
                        f"  {table}: skipped {dropped} Celigo row(s) — "
                        "connect Celigo in Settings -> Integrations, or re-run with --allow-celigo"
                    )

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
    parser.add_argument(
        "--allow-celigo",
        action="store_true",
        help=(
            "Import celigo/celigo_mcp rows too. Skipped by default: this script writes "
            "below the ORM, so the Celigo write guard cannot check the pair is coherent."
        ),
    )
    args = parser.parse_args()

    asyncio.run(import_tenant(args.input, args.dry_run, args.allow_celigo))


if __name__ == "__main__":
    main()
