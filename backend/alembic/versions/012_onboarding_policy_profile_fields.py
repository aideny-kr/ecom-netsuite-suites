"""Add onboarding policy/profile versioning fields

Revision ID: 012_onboarding_policy_fields
Revises: 011_onboarding_checklist
Create Date: 2026-02-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "012_onboarding_policy_fields"
down_revision = "011_onboarding_checklist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tenant_profiles
    op.add_column("tenant_profiles", sa.Column("team_size", sa.String(length=20), nullable=True))

    # policy_profiles
    op.add_column(
        "policy_profiles",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "policy_profiles",
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "policy_profiles",
        sa.Column("sensitivity_default", sa.String(length=32), nullable=False, server_default="financial"),
    )
    op.add_column("policy_profiles", sa.Column("tool_allowlist", postgresql.JSON(astext_type=sa.Text()), nullable=True))

    # Backfill deterministic policy versions for existing rows before adding uniqueness.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT id, ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY created_at ASC, id ASC) AS rn
              FROM policy_profiles
            )
            UPDATE policy_profiles AS p
            SET version = ranked.rn
            FROM ranked
            WHERE p.id = ranked.id
            """
        )
    )

    op.create_unique_constraint(
        "uq_policy_profiles_tenant_version",
        "policy_profiles",
        ["tenant_id", "version"],
    )

    # Keep model-level defaults as source of truth after migration backfill.
    op.alter_column("policy_profiles", "version", server_default=None)
    op.alter_column("policy_profiles", "is_locked", server_default=None)
    op.alter_column("policy_profiles", "sensitivity_default", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_policy_profiles_tenant_version", "policy_profiles", type_="unique")
    op.drop_column("policy_profiles", "tool_allowlist")
    op.drop_column("policy_profiles", "sensitivity_default")
    op.drop_column("policy_profiles", "is_locked")
    op.drop_column("policy_profiles", "version")
    op.drop_column("tenant_profiles", "team_size")
