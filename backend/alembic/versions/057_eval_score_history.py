"""Eval score history — tracks nightly improvement loop results over time."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "057_eval_score"
down_revision = "056_ops_perms"


def upgrade() -> None:
    op.create_table(
        "eval_score_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("dialect", sa.String(20), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("kept", sa.Integer(), nullable=False),
        sa.Column("reverted", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("avg_composite_score", sa.Float(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("eval_score_history")
