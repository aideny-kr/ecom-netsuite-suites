"""Persist and fence the budget for each approved external operation.

Revision ID: 100_tx_operation_budgets
Revises: 099_transaction_ops_state
"""

import sqlalchemy as sa

from alembic import op

revision = "100_tx_operation_budgets"
down_revision = "099_transaction_ops_state"
branch_labels = None
depends_on = None

TABLE = "transaction_ops_operations"


def upgrade():
    op.execute(f"DROP TRIGGER transaction_ops_immutable ON {TABLE}")
    op.add_column(TABLE, sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("max_api_calls", sa.Integer(), server_default="96", nullable=False))
    op.add_column(TABLE, sa.Column("api_calls_used", sa.Integer(), server_default="0", nullable=False))
    # Earlier attempts cannot acquire a fresh budget. Exhaust their spend and
    # preserve their original start time; recovery determines unknown vs failed.
    op.execute(f"UPDATE {TABLE} SET deadline_at = attempted_at + interval '300 seconds', api_calls_used = 96")
    op.alter_column(TABLE, "deadline_at", nullable=False)
    op.create_check_constraint(
        "ck_tx_operation_spend",
        TABLE,
        "max_api_calls BETWEEN 1 AND 96 AND api_calls_used >= 0 AND api_calls_used <= max_api_calls",
    )
    op.execute("""
        CREATE FUNCTION transaction_ops_operation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE mutable text[] := ARRAY['status','completed_at','result_json','updated_at','api_calls_used'];
        BEGIN
            IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable)
                OR NEW.api_calls_used < OLD.api_calls_used THEN
                RAISE EXCEPTION 'immutable operation budget or evidence';
            END IF;
            IF (OLD.status IN ('verified','failed') AND
                (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at'))
                OR (OLD.status = 'unknown' AND NEW.status = 'executing') THEN
                RAISE EXCEPTION 'immutable operation attempt';
            END IF;
            IF OLD.result_json->'dispatch_reserved' = 'true'::jsonb AND (
                NEW.result_json->'dispatch_reserved' IS DISTINCT FROM OLD.result_json->'dispatch_reserved'
                OR NEW.result_json->'provider' IS DISTINCT FROM OLD.result_json->'provider'
                OR NEW.result_json->'payload_fingerprint' IS DISTINCT FROM OLD.result_json->'payload_fingerprint'
                OR NEW.result_json->'dispatch_reserved_at' IS DISTINCT FROM OLD.result_json->'dispatch_reserved_at'
            ) THEN
                RAISE EXCEPTION 'immutable operation dispatch';
            END IF;
            IF OLD.result_json->'dispatch_reserved' IS DISTINCT FROM 'true'::jsonb
                AND NEW.result_json->'dispatch_reserved' = 'true'::jsonb
                AND (OLD.status <> 'executing' OR NEW.status <> 'executing'
                     OR NEW.api_calls_used <> OLD.api_calls_used + 1) THEN
                RAISE EXCEPTION 'dispatch requires a single budgeted execution';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION transaction_ops_operation_guard()"
    )


def downgrade():
    op.execute(f"DROP TRIGGER transaction_ops_immutable ON {TABLE}")
    op.execute("DROP FUNCTION transaction_ops_operation_guard()")
    op.drop_constraint("ck_tx_operation_spend", TABLE, type_="check")
    for column in ("api_calls_used", "max_api_calls", "deadline_at"):
        op.drop_column(TABLE, column)
    op.execute(
        f"CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION transaction_ops_guard()"
    )
