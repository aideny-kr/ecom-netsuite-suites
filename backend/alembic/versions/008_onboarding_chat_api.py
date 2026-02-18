"""AI Onboarding (tenant profiles, policies, prompt templates) + Chat Integration API keys

Revision ID: 008_onboarding_chat_api
Revises: 007_dev_workspace
Create Date: 2026-02-17
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "008_onboarding_chat_api"
down_revision = "007_dev_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- tenant_profiles ---
    op.create_table(
        "tenant_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, default=1),
        sa.Column("status", sa.String(20), nullable=False, default="draft"),
        sa.Column("industry", sa.String(100), nullable=True),
        sa.Column("business_description", sa.Text, nullable=True),
        sa.Column("netsuite_account_id", sa.String(100), nullable=True),
        sa.Column("chart_of_accounts", sa.JSON, nullable=True),
        sa.Column("subsidiaries", sa.JSON, nullable=True),
        sa.Column("item_types", sa.JSON, nullable=True),
        sa.Column("custom_segments", sa.JSON, nullable=True),
        sa.Column("fiscal_calendar", sa.JSON, nullable=True),
        sa.Column("suiteql_naming", sa.JSON, nullable=True),
        sa.Column("confirmed_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tenant_profiles_tenant_id", "tenant_profiles", ["tenant_id"])
    op.create_unique_constraint("uq_tenant_profiles_tenant_version", "tenant_profiles", ["tenant_id", "version"])

    # RLS for tenant_profiles
    op.execute("ALTER TABLE tenant_profiles ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_profiles_isolation ON tenant_profiles "
        "USING (tenant_id::text = current_setting('app.current_tenant_id', true))"
    )

    # --- policy_profiles ---
    op.create_table(
        "policy_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("read_only_mode", sa.Boolean, default=True, nullable=False),
        sa.Column("allowed_record_types", sa.JSON, nullable=True),
        sa.Column("blocked_fields", sa.JSON, nullable=True),
        sa.Column("max_rows_per_query", sa.Integer, default=1000, nullable=False),
        sa.Column("require_row_limit", sa.Boolean, default=True, nullable=False),
        sa.Column("custom_rules", sa.JSON, nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_policy_profiles_tenant_id", "policy_profiles", ["tenant_id"])

    # RLS for policy_profiles
    op.execute("ALTER TABLE policy_profiles ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY policy_profiles_isolation ON policy_profiles "
        "USING (tenant_id::text = current_setting('app.current_tenant_id', true))"
    )

    # --- system_prompt_templates ---
    op.create_table(
        "system_prompt_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, default=1),
        sa.Column("profile_id", UUID(as_uuid=True), sa.ForeignKey("tenant_profiles.id"), nullable=False),
        sa.Column("policy_id", UUID(as_uuid=True), sa.ForeignKey("policy_profiles.id"), nullable=True),
        sa.Column("template_text", sa.Text, nullable=False),
        sa.Column("sections", sa.JSON, nullable=True),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_system_prompt_templates_tenant_id", "system_prompt_templates", ["tenant_id"])

    # RLS for system_prompt_templates
    op.execute("ALTER TABLE system_prompt_templates ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY system_prompt_templates_isolation ON system_prompt_templates "
        "USING (tenant_id::text = current_setting('app.current_tenant_id', true))"
    )

    # --- chat_api_keys ---
    op.create_table(
        "chat_api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(10), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", sa.JSON, nullable=True),
        sa.Column("rate_limit_per_minute", sa.Integer, default=60, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_api_keys_tenant_id", "chat_api_keys", ["tenant_id"])
    op.create_index("ix_chat_api_keys_key_hash", "chat_api_keys", ["key_hash"])

    # RLS for chat_api_keys
    op.execute("ALTER TABLE chat_api_keys ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY chat_api_keys_isolation ON chat_api_keys "
        "USING (tenant_id::text = current_setting('app.current_tenant_id', true))"
    )

    # --- Alter tenant_configs: add onboarding_completed_at ---
    op.add_column("tenant_configs", sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True))

    # --- Seed new permissions ---
    new_permissions = [
        "onboarding.manage",
        "onboarding.view",
        "chat_api.manage",
        "chat_api.use",
        "policy.manage",
        "policy.view",
    ]
    for codename in new_permissions:
        perm_id = uuid.uuid4()
        op.execute(
            sa.text(
                "INSERT INTO permissions (id, codename) VALUES (:id, :codename) ON CONFLICT (codename) DO NOTHING"
            ).bindparams(id=perm_id, codename=codename)
        )

    # Assign all new permissions to admin role
    for codename in new_permissions:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM roles r, permissions p "
                "WHERE r.name = 'admin' AND p.codename = :codename "
                "ON CONFLICT DO NOTHING"
            ).bindparams(codename=codename)
        )


def downgrade() -> None:
    # Drop RLS policies
    op.execute("DROP POLICY IF EXISTS chat_api_keys_isolation ON chat_api_keys")
    op.execute("DROP POLICY IF EXISTS system_prompt_templates_isolation ON system_prompt_templates")
    op.execute("DROP POLICY IF EXISTS policy_profiles_isolation ON policy_profiles")
    op.execute("DROP POLICY IF EXISTS tenant_profiles_isolation ON tenant_profiles")

    # Drop tables in reverse FK order
    op.drop_table("chat_api_keys")
    op.drop_table("system_prompt_templates")
    op.drop_table("policy_profiles")
    op.drop_table("tenant_profiles")

    # Remove added column
    op.drop_column("tenant_configs", "onboarding_completed_at")

    # Remove seeded permissions (and role_permissions cascade)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE codename IN "
        "('onboarding.manage','onboarding.view','chat_api.manage','chat_api.use','policy.manage','policy.view'))"
    )
    op.execute(
        "DELETE FROM permissions WHERE codename IN "
        "('onboarding.manage','onboarding.view','chat_api.manage','chat_api.use','policy.manage','policy.view')"
    )
