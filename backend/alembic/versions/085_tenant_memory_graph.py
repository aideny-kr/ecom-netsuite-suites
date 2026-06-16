"""tenant memory graph — concept/edge/link tables + HNSW + FORCE RLS + memory.manage perm

Three tenant-scoped, RLS-isolated tables overlaying the existing learning tables
(tenant_learned_rules / tenant_query_patterns), never modifying them:

  * tenant_memory_concept — plain-English business concepts with a trust spine
    (review_state / confidence / confirmed_by) + an embedding for future fuzzy dedup.
  * tenant_memory_edge    — directed, named relationships between concepts.
  * tenant_memory_link    — evidence: which source learning row a concept came from;
    the (tenant_id, source_table, source_id) unique constraint is the backfill
    idempotency key.

RLS mirrors the metric_definitions WITH-CHECK idiom (082_metric_def_with_check) +
FORCE (the app role is NOT BYPASSRLS on Supabase): ENABLE + FORCE + a policy whose
USING and WITH CHECK both pin tenant_id to get_current_tenant_id(). No SYSTEM-default
rows here, so there is no OR-SYSTEM read branch.

Permission seeding copies 080_metric_definitions.py verbatim (real schema:
permissions(id, codename); role_permissions(role_id, permission_id) joined to roles
on r.name = 'admin') — NOT the (codename, description) / literal-'admin' shape some
research drafts guessed.

Chains off 082_metric_def_with_check, the single live head in this branch (there is no
084_reports here). One linear history — no merge migration (a merge head fails the deploy
`downgrade -1` reversibility gate with "Ambiguous walk").
"""

import uuid

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "085_tenant_memory_graph"
down_revision = "082_metric_def_with_check"
branch_labels = None
depends_on = None

# Drop order (children before parents) for downgrade.
_TABLES = ("tenant_memory_link", "tenant_memory_edge", "tenant_memory_concept")
_RLS_TABLES = ("tenant_memory_concept", "tenant_memory_edge", "tenant_memory_link")


def upgrade() -> None:
    op.create_table(
        "tenant_memory_concept",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("concept_type", sa.String(50), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("review_state", sa.String(20), server_default="pending", nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("origin_session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("origin_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_by", UUID(as_uuid=True), nullable=True),
        sa.Column("merged_into_id", UUID(as_uuid=True), nullable=True),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["merged_into_id"], ["tenant_memory_concept.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_memory_concept_tenant_id", "tenant_memory_concept", ["tenant_id"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tmc_embedding ON tenant_memory_concept "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
    )

    op.create_table(
        "tenant_memory_edge",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_concept_id", UUID(as_uuid=True), nullable=False),
        sa.Column("target_concept_id", UUID(as_uuid=True), nullable=False),
        sa.Column("relation", sa.String(100), nullable=False),
        sa.Column("review_state", sa.String(20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_concept_id"], ["tenant_memory_concept.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_concept_id"], ["tenant_memory_concept.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_memory_edge_tenant_id", "tenant_memory_edge", ["tenant_id"])

    op.create_table(
        "tenant_memory_link",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("concept_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_table", sa.String(50), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["concept_id"], ["tenant_memory_concept.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_table", "source_id", name="uq_tenant_memory_link_source"),
    )
    op.create_index("ix_tenant_memory_link_tenant_id", "tenant_memory_link", ["tenant_id"])

    # RLS — FORCE + USING + WITH CHECK (app role is NOT BYPASSRLS on Supabase). Both
    # clauses pin tenant_id to the caller's active tenant context (no OR-SYSTEM branch).
    for tbl in _RLS_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {tbl}_tenant_isolation ON {tbl} "
            f"USING (tenant_id = get_current_tenant_id()) "
            f"WITH CHECK (tenant_id = get_current_tenant_id())"
        )

    # Permission + grant to the tenant 'admin' role (idempotent) — verbatim from
    # 080_metric_definitions.py:74-86 (real schema, not a guessed one).
    op.execute(
        sa.text(
            "INSERT INTO permissions (id, codename) VALUES (:id, :codename) ON CONFLICT (codename) DO NOTHING"
        ).bindparams(id=uuid.uuid4(), codename="memory.manage")
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.name = 'admin' AND p.codename = :codename ON CONFLICT DO NOTHING"
        ).bindparams(codename="memory.manage")
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE codename='memory.manage')"
    )
    op.execute("DELETE FROM permissions WHERE codename='memory.manage'")
    for tbl in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.drop_table(tbl)
