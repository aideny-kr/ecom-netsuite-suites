"""Eval cases table for autonomous query improvement loop."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "055_eval_cases"
down_revision = "054_experiment_log"


def upgrade() -> None:
    op.create_table(
        "eval_cases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("dialect", sa.String(20), nullable=False),
        sa.Column("expected_keywords", JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source", sa.String(20), nullable=False, server_default=sa.text("'organic'")),
        sa.Column("source_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("generated_sql", sa.Text, nullable=True),
        sa.Column("confidence_score", sa.Float, nullable=True),
        sa.Column("times_tested", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_score", sa.Float, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_eval_cases_tenant_dialect", "eval_cases", ["tenant_id", "dialect"])


def downgrade() -> None:
    op.drop_index("ix_eval_cases_tenant_dialect", table_name="eval_cases")
    op.drop_table("eval_cases")
