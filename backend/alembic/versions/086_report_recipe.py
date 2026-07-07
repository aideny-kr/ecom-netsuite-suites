"""Add reports.recipe_json — the captured refresh recipe (Slice A of live-dashboard reports).

Nullable, NO server_default — historic reports legitimately lack a recipe and stay
snapshot-only (spec §4A: no backfill; the UI shows Refresh only when a recipe exists).
Shape (schema_version 1): {"schema_version": 1, "captured_at": <utc iso8601>,
"sections": [...the LLM's compose sections VERBATIM, pre-resolution...],
"sources": {rid: {"tool": <executed tool name>, "params": <executed params>,
"connection_id": <str uuid for ext__ tools | null for local tools>}}}.
Server-captured from EXECUTED tool calls only (never model-authored post-hoc);
read-only allowlisted tools only, fail closed (spec §4A trust boundary).

RLS: reports is already ENABLE+FORCE row-level-secured with the tenant policy
(084_reports) — a new column inherits it; no policy change needed.

Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "086_report_recipe"
down_revision = "085_tenant_memory_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("recipe_json", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("reports", "recipe_json")
