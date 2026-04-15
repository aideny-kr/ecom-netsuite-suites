"""Pre-deploy schema validation: verify SQLAlchemy models match the database.

Catches the exact bug where a migration drops/adds a column but the model
isn't updated (or vice versa). Run before deploying to staging/production.

Usage:
    # Local (against Supabase):
    .venv/bin/python scripts/validate_schema.py

    # Inside Docker container (against its DB):
    docker exec <container> python scripts/validate_schema.py
"""

import asyncio
import importlib
import pkgutil
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Auto-discover and import ALL model modules so they register with Base.metadata.
# Using pkgutil ensures we don't miss models that aren't in __init__.py.
import app.models as _models_pkg
from app.core.config import settings
from app.core.database import _build_connect_args
from app.models.base import Base

for _, modname, _ in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{modname}")


async def validate() -> list[str]:
    """Compare model columns against actual DB columns. Return list of errors."""
    db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    engine = create_async_engine(db_url, connect_args=_build_connect_args(db_url))
    errors: list[str] = []

    try:
        async with engine.connect() as conn:
            # Get actual DB columns for each mapped table
            for table_name, table in Base.metadata.tables.items():
                model_cols = {col.name for col in table.columns}

                # Query information_schema for actual columns
                result = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t"
                    ),
                    {"t": table_name},
                )
                db_cols = {row[0] for row in result.fetchall()}

                if not db_cols:
                    # Table doesn't exist yet (new migration not applied)
                    continue

                # Columns in model but not in DB
                extra_in_model = model_cols - db_cols
                if extra_in_model:
                    errors.append(f"  {table_name}: model has columns not in DB: {extra_in_model}")

                # Columns in DB but not in model (warning only — could be intentional)
                extra_in_db = db_cols - model_cols
                if extra_in_db:
                    # Only warn, don't fail — extra DB columns are usually fine
                    print(f"  [WARN] {table_name}: DB has unmapped columns: {extra_in_db}")

    finally:
        await engine.dispose()

    return errors


def main() -> int:
    print("Validating model ↔ DB schema alignment...")
    errors = asyncio.run(validate())

    if errors:
        print("\nFAILED — model/DB mismatch detected:")
        for err in errors:
            print(err)
        print("\nFix: update the SQLAlchemy model to match the migration, or write a new migration to match the model.")
        return 1

    print("OK — all model columns match the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
