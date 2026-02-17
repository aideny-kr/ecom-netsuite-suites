"""Dev Workspace tables: workspaces, workspace_files, workspace_changesets, workspace_patches

Revision ID: 007_dev_workspace
Revises: 006_chat_is_byok
Create Date: 2026-02-17
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "007_dev_workspace"
down_revision = "006_chat_is_byok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pg_trgm extension for trigram search
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # --- workspaces ---
    op.create_table(
        "workspaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), server_default="active", nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])

    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY workspaces_tenant_isolation ON workspaces
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # --- workspace_files ---
    op.create_table(
        "workspace_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.Integer, server_default="0", nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("sha256_hash", sa.String(64), nullable=True),
        sa.Column("is_directory", sa.Boolean, server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_files_tenant_id", "workspace_files", ["tenant_id"])
    op.create_index("ix_workspace_files_workspace_id", "workspace_files", ["workspace_id"])
    op.create_unique_constraint("uq_workspace_files_workspace_path", "workspace_files", ["workspace_id", "path"])

    # Trigram GIN indexes for ILIKE search
    op.execute("CREATE INDEX ix_workspace_files_path_trgm ON workspace_files USING GIN (path gin_trgm_ops)")
    op.execute("CREATE INDEX ix_workspace_files_content_trgm ON workspace_files USING GIN (content gin_trgm_ops)")

    op.execute("ALTER TABLE workspace_files ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY workspace_files_tenant_isolation ON workspace_files
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # --- workspace_changesets ---
    op.create_table(
        "workspace_changesets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), server_default="draft", nullable=False),
        sa.Column("proposed_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reviewed_by", UUID(as_uuid=True), nullable=True),
        sa.Column("applied_by", UUID(as_uuid=True), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_changesets_tenant_id", "workspace_changesets", ["tenant_id"])
    op.create_index("ix_workspace_changesets_workspace_id", "workspace_changesets", ["workspace_id"])

    op.execute("ALTER TABLE workspace_changesets ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY workspace_changesets_tenant_isolation ON workspace_changesets
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # --- workspace_patches ---
    op.create_table(
        "workspace_patches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("changeset_id", UUID(as_uuid=True), sa.ForeignKey("workspace_changesets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("unified_diff", sa.Text, nullable=True),
        sa.Column("new_content", sa.Text, nullable=True),
        sa.Column("baseline_sha256", sa.String(64), nullable=False),
        sa.Column("apply_order", sa.Integer, server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_patches_tenant_id", "workspace_patches", ["tenant_id"])
    op.create_index("ix_workspace_patches_changeset_id", "workspace_patches", ["changeset_id"])

    op.execute("ALTER TABLE workspace_patches ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY workspace_patches_tenant_isolation ON workspace_patches
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # --- Permissions ---
    op.execute("""
        INSERT INTO permissions (id, codename)
        VALUES
            (gen_random_uuid(), 'workspace.manage'),
            (gen_random_uuid(), 'workspace.view'),
            (gen_random_uuid(), 'workspace.review'),
            (gen_random_uuid(), 'workspace.apply')
        ON CONFLICT (codename) DO NOTHING
    """)

    # Grant workspace permissions to admin role
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r
        CROSS JOIN permissions p
        WHERE r.name = 'admin'
          AND p.codename IN ('workspace.manage', 'workspace.view', 'workspace.review', 'workspace.apply')
        ON CONFLICT DO NOTHING
    """)

    # Grant workspace.view to readonly role
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r
        CROSS JOIN permissions p
        WHERE r.name = 'readonly'
          AND p.codename = 'workspace.view'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # Drop RLS policies
    op.execute("DROP POLICY IF EXISTS workspace_patches_tenant_isolation ON workspace_patches")
    op.execute("ALTER TABLE workspace_patches DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_changesets_tenant_isolation ON workspace_changesets")
    op.execute("ALTER TABLE workspace_changesets DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_files_tenant_isolation ON workspace_files")
    op.execute("ALTER TABLE workspace_files DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspaces_tenant_isolation ON workspaces")
    op.execute("ALTER TABLE workspaces DISABLE ROW LEVEL SECURITY")

    # Drop tables in reverse order
    op.drop_table("workspace_patches")
    op.drop_table("workspace_changesets")
    op.drop_table("workspace_files")
    op.drop_table("workspaces")

    # Clean up permissions
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions
            WHERE codename IN ('workspace.manage', 'workspace.view', 'workspace.review', 'workspace.apply')
        )
    """)
    op.execute("""
        DELETE FROM permissions
        WHERE codename IN ('workspace.manage', 'workspace.view', 'workspace.review', 'workspace.apply')
    """)
