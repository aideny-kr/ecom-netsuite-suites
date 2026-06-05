"""Defense-in-depth: ensure the synthetic SYSTEM tenant parent row exists.

SYSTEM-default metric rows (``tenant_id = SYSTEM_TENANT_ID``) FK to ``tenants.id``.
Migration 080 provisions this row on a fresh DB, but the seeder + authoring CLI must
be self-sufficient (callable against any DB state without a separate bootstrap step),
so they upsert the SYSTEM tenant first. Idempotent via ``ON CONFLICT (id) DO NOTHING``.
Mirrors ``app/models/tenant.py`` NOT NULL columns (name, slug, plan, is_active).
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import SYSTEM_TENANT_ID


async def ensure_system_tenant(db: AsyncSession) -> None:
    await db.execute(
        text(
            "INSERT INTO tenants (id, name, slug, plan, is_active) "
            "VALUES (CAST(:id AS uuid), :name, :slug, :plan, :is_active) ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=str(SYSTEM_TENANT_ID), name="System", slug="system", plan="free", is_active=True)
    )
