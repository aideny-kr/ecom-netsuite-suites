"""Experiment log for autonomous query improvement."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "054_experiment_log"
down_revision = "053_mcp_financial_flag"


def upgrade() -> None:
    op.create_table(
        "experiment_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("dialect", sa.String(20), nullable=False),
        sa.Column("hypothesis", sa.Text, nullable=False),
        sa.Column("test_query", sa.Text, nullable=False),
        sa.Column("generated_sql", sa.Text, nullable=True),
        sa.Column("executed_successfully", sa.Boolean, nullable=True),
        sa.Column("score_accuracy", sa.Float, nullable=True),
        sa.Column("score_syntax", sa.Float, nullable=True),
        sa.Column("score_efficiency", sa.Float, nullable=True),
        sa.Column("experiment_score", sa.Float, nullable=True),
        sa.Column("baseline_score", sa.Float, nullable=True),
        sa.Column("delta", sa.Float, nullable=True),
        sa.Column("decision", sa.String(10), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("experiment_log")
