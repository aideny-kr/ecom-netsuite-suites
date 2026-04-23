"""Enable the `drive_rag` feature flag for specific tenants.

Usage (from backend/):
    .venv/bin/python scripts/seed_drive_rag_flag.py <tenant_id> [<tenant_id>...]

On staging (from local host):
    ssh aidenyi@34.73.236.64 \\
      "sudo docker exec ecom-netsuite-backend-1 \\
       python scripts/seed_drive_rag_flag.py <tenant_id>"

The `drive_rag` flag defaults to False in DEFAULT_FLAGS (phased rollout).
This script flips it to True for tenants who should have access. New
tenants created after the flag was added already have a row with
enabled=False — this script updates that row in place. Existing tenants
from before the flag was added get a new row inserted.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.feature_flag import TenantFeatureFlag


async def main(tenant_ids: list[str]) -> None:
    async with async_session_factory() as db:
        for tid in tenant_ids:
            try:
                tenant_uuid = uuid.UUID(tid)
            except ValueError:
                print(f"skip {tid}: not a valid UUID")
                continue

            existing = (
                await db.execute(
                    select(TenantFeatureFlag).where(
                        TenantFeatureFlag.tenant_id == tenant_uuid,
                        TenantFeatureFlag.flag_key == "drive_rag",
                    )
                )
            ).scalars().first()
            if existing:
                if existing.enabled:
                    print(f"noop {tid}: drive_rag already enabled")
                else:
                    existing.enabled = True
                    print(f"updated {tid}: drive_rag=true")
            else:
                db.add(
                    TenantFeatureFlag(
                        tenant_id=tenant_uuid,
                        flag_key="drive_rag",
                        enabled=True,
                    )
                )
                print(f"seeded {tid}: drive_rag=true")
        await db.commit()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: seed_drive_rag_flag.py <tenant_id> [<tenant_id>...]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1:]))
