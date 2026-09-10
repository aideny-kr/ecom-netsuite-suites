"""Durable cases and immutable observations across reconciliation runs."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "106_transaction_cases"
down_revision = "105_tx_config_revisions"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.create_table(
        "transaction_cases",
        *common(),
        sa.Column("case_key", sa.String(64), nullable=False),
        sa.Column("order_reference", sa.String(100), nullable=False),
        sa.Column("scope_json", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_report_json", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "case_key"),
        sa.CheckConstraint("status IN ('open','reconciled')", name="ck_transaction_case_status"),
    )
    op.create_table(
        "transaction_case_observations",
        *common(),
        sa.Column("case_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_key", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_json", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("tenant_id", "observation_key"),
        sa.ForeignKeyConstraint(["tenant_id", "case_id"], ["transaction_cases.tenant_id", "transaction_cases.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"]),
    )
    for table in ("transaction_cases", "transaction_case_observations"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = get_current_tenant_id()) "
            "WITH CHECK (tenant_id = get_current_tenant_id())"
        )
    op.create_index("ix_transaction_case_observations_case_id", "transaction_case_observations", ["case_id"])
    op.execute("""CREATE FUNCTION transaction_case_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_TABLE_NAME = 'transaction_case_observations' OR TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'immutable transaction case observation';
        END IF;
        IF (to_jsonb(NEW) - ARRAY['status','last_observed_at','latest_report_json','updated_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['status','last_observed_at','latest_report_json','updated_at']) OR
           NEW.last_observed_at < OLD.last_observed_at THEN
            RAISE EXCEPTION 'immutable transaction case identity';
        END IF;
        RETURN NEW;
    END $$""")
    for table in ("transaction_cases", "transaction_case_observations"):
        op.execute(
            f"CREATE TRIGGER transaction_case_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION transaction_case_guard()"
        )


def downgrade():
    op.drop_table("transaction_case_observations")
    op.drop_table("transaction_cases")
    op.execute("DROP FUNCTION transaction_case_guard()")
