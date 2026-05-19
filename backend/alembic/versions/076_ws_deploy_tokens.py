"""workspace_deploy_tokens table for two-step gated sandbox deploy

Revision ID: 076_ws_deploy_tokens
Revises: 074_validation_hits
Create Date: 2026-05-18

Stores per-preview tokens for the two-step gated sandbox deploy flow.
Each row binds (tenant, workspace, changeset, sandbox_id, snapshot_sha,
manifest_sha, require_assertions, actor_id, issued_at) so the HMAC token
returned by the preview endpoint can be replay-checked at confirm time.

The partial unique index ``uq_deploy_inflight`` blocks two concurrent
in-flight previews for the same changeset (codex P1 #3 — double-click
queueing multiple WorkspaceRun rows).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers
revision = "076_ws_deploy_tokens"
down_revision = "074_validation_hits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_deploy_tokens",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "changeset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspace_changesets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sandbox_id", sa.Text(), nullable=False),
        sa.Column("snapshot_sha", sa.String(64), nullable=False),
        sa.Column("manifest_sha", sa.String(64), nullable=False),
        sa.Column("require_assertions", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consumed_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspace_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("consumed_reason", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_workspace_deploy_tokens_tenant_id",
        "workspace_deploy_tokens",
        ["tenant_id"],
    )
    op.create_index(
        "ix_workspace_deploy_tokens_lookup",
        "workspace_deploy_tokens",
        ["tenant_id", "changeset_id", "consumed_at"],
    )
    op.create_index(
        "ix_workspace_deploy_tokens_expires_at",
        "workspace_deploy_tokens",
        ["expires_at"],
    )
    # Partial unique constraint — at most one unconsumed token per
    # (tenant, changeset). Postgres rejects now() in index predicates
    # (non-IMMUTABLE), so TTL enforcement happens at confirm time in the
    # app layer: an expired token is treated as consumed by writing
    # consumed_at + consumed_reason="expired" before any new preview
    # mints. That frees the unique slot.
    op.create_index(
        "uq_deploy_inflight",
        "workspace_deploy_tokens",
        ["tenant_id", "changeset_id"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_deploy_inflight", table_name="workspace_deploy_tokens")
    op.drop_index(
        "ix_workspace_deploy_tokens_expires_at",
        table_name="workspace_deploy_tokens",
    )
    op.drop_index(
        "ix_workspace_deploy_tokens_lookup",
        table_name="workspace_deploy_tokens",
    )
    op.drop_index(
        "ix_workspace_deploy_tokens_tenant_id",
        table_name="workspace_deploy_tokens",
    )
    op.drop_table("workspace_deploy_tokens")
