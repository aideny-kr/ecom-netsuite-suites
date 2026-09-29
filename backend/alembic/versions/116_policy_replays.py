"""Separate immutable historical policy receipts from operational reconciliation."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "116_policy_replays"
down_revision = "115_review_read_projection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transaction_policy_replays",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("tenant_id", pg.UUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("evaluation_key", pg.UUID(), nullable=False),
        sa.Column("source_config_id", pg.UUID(), nullable=False),
        sa.Column("target_config_id", pg.UUID(), nullable=False),
        sa.Column("initiated_by", pg.UUID(), sa.ForeignKey("users.id"), nullable=False),
        *(
            sa.Column(key, pg.JSONB(), nullable=False)
            for key in ("request_json", "source_snapshot", "target_snapshot", "manifest_json")
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "evaluation_key"),
        *(
            sa.ForeignKeyConstraint(
                ["tenant_id", key], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
            )
            for key in ("source_config_id", "target_config_id")
        ),
        sa.CheckConstraint("status IN ('pending','finished','cancelled')", name="ck_policy_replay_status"),
    )
    op.create_index("ix_transaction_policy_replays_tenant_id", "transaction_policy_replays", ["tenant_id"])
    op.create_table(
        "transaction_policy_replay_entries",
        sa.Column("tenant_id", pg.UUID(), primary_key=True),
        sa.Column("replay_id", pg.UUID(), primary_key=True),
        sa.Column("order_reference", sa.String(100), primary_key=True),
        sa.Column("run_id", pg.UUID(), nullable=False),
        sa.Column("finding_id", pg.UUID(), nullable=False),
        sa.Column("original_hash", sa.String(64), nullable=False),
        sa.Column("result_json", pg.JSONB()),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "replay_id"], ["transaction_policy_replays.tenant_id", "transaction_policy_replays.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "order_reference"],
            [
                "transaction_ops_findings.tenant_id",
                "transaction_ops_findings.run_id",
                "transaction_ops_findings.order_reference",
            ],
        ),
    )
    op.create_index(
        "ix_policy_replay_pending",
        "transaction_policy_replay_entries",
        ["tenant_id", "replay_id", "order_reference"],
        postgresql_where=sa.text("result_json IS NULL"),
    )
    for table in ("transaction_policy_replays", "transaction_policy_replay_entries"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = get_current_tenant_id()) WITH CHECK (tenant_id = get_current_tenant_id())"
        )
    op.execute("""
        CREATE FUNCTION protect_policy_replay() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_TABLE_NAME = 'transaction_policy_replay_entries' THEN
            IF OLD.result_json IS NOT NULL OR
               (to_jsonb(NEW) - 'result_json' - 'evaluated_at') IS DISTINCT FROM
               (to_jsonb(OLD) - 'result_json' - 'evaluated_at') THEN
              RAISE EXCEPTION 'immutable policy replay entry';
            END IF;
          ELSIF OLD.status <> 'pending' OR NEW.processed < OLD.processed OR
            (to_jsonb(NEW) - 'status' - 'processed' - 'finished_at' - 'updated_at' - 'last_error_code') IS DISTINCT FROM
            (to_jsonb(OLD) - 'status' - 'processed' - 'finished_at' - 'updated_at' - 'last_error_code') THEN
            RAISE EXCEPTION 'immutable policy replay manifest';
          END IF;
          RETURN NEW;
        END $$;
    """)
    for table in ("transaction_policy_replays", "transaction_policy_replay_entries"):
        op.execute(
            f"CREATE TRIGGER policy_replay_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_policy_replay()"
        )


def downgrade():
    op.drop_table("transaction_policy_replay_entries")
    op.drop_table("transaction_policy_replays")
    op.execute("DROP FUNCTION protect_policy_replay()")
