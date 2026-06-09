"""metric definitions catalog (table + HNSW + RLS + metrics.manage perm)"""

import uuid

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "080_metric_definitions"
# Chained AFTER 080_learned_rules_rls (re-parented from 079) so the metric line and main's
# learned-rules-RLS line form ONE linear history instead of two parallel heads off 079.
# This replaces the former 083 merge migration: a merge head makes `alembic downgrade -1`
# (the deploy migration-safety reversibility test) fail with "Ambiguous walk". The two
# lineages touch disjoint tables (metric_definitions vs tenant_learned_rules), so ordering
# one after the other is behaviourally identical to merging them.
down_revision = "080_learned_rules_rls"
branch_labels = None
depends_on = None

SYSTEM_TENANT = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    # SYSTEM-default metric rows (tenant_id = SYSTEM_TENANT) FK to tenants.id. The
    # synthetic SYSTEM tenant row does not exist on a fresh DB, so provision it
    # BEFORE the table/seeder can reference it — otherwise the seeder FK-violates.
    # Idempotent: ON CONFLICT (id) DO NOTHING. Mirrors app/models/tenant.py NOT NULL
    # columns (name, slug, plan, is_active). 080 is not on remote yet, so it only
    # runs on fresh DBs.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, slug, plan, is_active) "
            "VALUES (CAST(:id AS uuid), :name, :slug, :plan, :is_active) ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=SYSTEM_TENANT, name="System", slug="system", plan="free", is_active=True)
    )
    op.create_table(
        "metric_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("blessed_spec", JSONB(), nullable=True),
        sa.Column("expression", sa.Text(), nullable=True),
        sa.Column("depends_on", ARRAY(sa.Text()), nullable=True),
        sa.Column("params_schema", JSONB(), nullable=True),
        sa.Column("dimensions", JSONB(), nullable=True),
        sa.Column("synonyms", ARRAY(sa.Text()), nullable=True),
        sa.Column("intent_embedding", Vector(1536), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provenance", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "key", name="uq_metric_tenant_key"),
    )
    op.create_index("ix_metric_definitions_tenant", "metric_definitions", ["tenant_id"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_metric_definitions_embedding_hnsw "
        "ON metric_definitions USING hnsw (intent_embedding vector_cosine_ops) "
        "WITH (m=16, ef_construction=64)"
    )
    # RLS — doc_chunks-style policy so SYSTEM-default rows are visible to every tenant.
    op.execute("ALTER TABLE metric_definitions ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY metric_definitions_tenant_isolation ON metric_definitions
        USING (tenant_id = get_current_tenant_id() OR tenant_id = '{SYSTEM_TENANT}'::uuid)
    """)
    # Permission + grant to the tenant 'admin' role (idempotent).
    op.execute(
        sa.text(
            "INSERT INTO permissions (id, codename) VALUES (:id, :codename) ON CONFLICT (codename) DO NOTHING"
        ).bindparams(id=uuid.uuid4(), codename="metrics.manage")
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.name = 'admin' AND p.codename = :codename ON CONFLICT DO NOTHING"
        ).bindparams(codename="metrics.manage")
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS metric_definitions_tenant_isolation ON metric_definitions")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE codename='metrics.manage')"
    )
    op.execute("DELETE FROM permissions WHERE codename='metrics.manage'")
    op.drop_table("metric_definitions")
