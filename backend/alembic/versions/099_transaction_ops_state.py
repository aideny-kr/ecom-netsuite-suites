"""Durable transaction operations, tenant RLS and immutable approval evidence."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "099_transaction_ops_state"
down_revision = "098_celigo_flow_errors_checked"
branch_labels = None
depends_on = None


def _common():
    return [
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _text(name, length=255, nullable=False):
    return sa.Column(name, sa.String(length), nullable=nullable)


def _uuid(name, target=None, nullable=False):
    return sa.Column(name, pg.UUID(as_uuid=True), *([sa.ForeignKey(target)] if target else []), nullable=nullable)


def _json(name):
    return sa.Column(name, pg.JSONB(), nullable=False)


def _time(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _number(name):
    return sa.Column(name, sa.Integer(), nullable=False)


def upgrade():
    op.create_table(
        "transaction_ops_configs",
        *_common(),
        _text("config_key", 64),
        _text("name"),
        _uuid("source_step_id", "celigo_flow_steps.id"),
        _uuid("netsuite_connection_id", "connections.id"),
        _text("netsuite_account_id"),
        _text("subsidiary_id"),
        _text("record_type", 50),
        _uuid("target_step_id", "celigo_flow_steps.id", True),
        _json("mapping_json"),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False),
        _number("interval_minutes"),
        _number("max_api_calls"),
        _number("max_orders"),
        _number("deadline_seconds"),
        _uuid("created_by", "users.id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "config_key"),
        sa.CheckConstraint("NOT schedule_enabled OR enabled", name="ck_tx_config_schedule"),
        sa.CheckConstraint(
            "max_api_calls BETWEEN 1 AND 2000 AND max_orders BETWEEN 1 AND 10000", name="ck_tx_config_budget"
        ),
        sa.CheckConstraint(
            "deadline_seconds BETWEEN 30 AND 3600 AND interval_minutes BETWEEN 5 AND 10080", name="ck_tx_config_clock"
        ),
    )
    op.create_table(
        "transaction_ops_runs",
        *_common(),
        _uuid("config_id"),
        _text("work_key", 64),
        _text("origin", 20),
        _json("params_json"),
        _json("config_snapshot"),
        _text("status", 20),
        _text("termination_reason", 20, True),
        _number("max_api_calls"),
        _number("max_orders"),
        _number("api_calls_used"),
        _number("orders_used"),
        _time("deadline_at"),
        _uuid("lease_token", nullable=True),
        _time("lease_until", True),
        _json("progress_json"),
        _uuid("initiated_by", "users.id", True),
        _time("finished_at", True),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "work_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "config_id"], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
        ),
        sa.CheckConstraint("status IN ('pending','running','finished')", name="ck_tx_run_status"),
        sa.CheckConstraint("origin IN ('manual','chat','schedule')", name="ck_tx_run_origin"),
        sa.CheckConstraint(
            "termination_reason IS NULL OR termination_reason IN ('done','budget','stall','error')",
            name="ck_tx_run_reason",
        ),
        sa.CheckConstraint(
            "api_calls_used >= 0 AND api_calls_used <= max_api_calls "
            "AND orders_used >= 0 AND orders_used <= max_orders",
            name="ck_tx_run_spend",
        ),
    )
    op.create_table(
        "transaction_ops_proposals",
        *_common(),
        _uuid("config_id"),
        _uuid("run_id"),
        _text("work_key", 64),
        _text("source_record_id"),
        _text("order_reference"),
        _text("target_record_id", nullable=True),
        _text("action", 40),
        _text("currency", 3),
        _text("netsuite_account_id"),
        _text("subsidiary_id"),
        _text("record_type", 50),
        _text("evidence_fingerprint", 64),
        _time("observed_at"),
        _time("valid_until"),
        _json("before_json"),
        _json("after_json"),
        _json("evidence_json"),
        _text("status", 20),
        _uuid("decided_by", "users.id", True),
        _time("decided_at", True),
        _text("decision_note", 2000, True),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "config_id"], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
        ),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"]),
        sa.CheckConstraint("status IN ('pending','approved','rejected','superseded')", name="ck_tx_proposal_status"),
        sa.CheckConstraint(
            "action IN ('sync_missing_order','correct_amounts','resolve_celigo_error')", name="ck_tx_proposal_action"
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved','rejected') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_tx_proposal_actor",
        ),
    )
    op.create_table(
        "transaction_ops_operations",
        *_common(),
        _uuid("proposal_id"),
        _text("work_key", 64),
        _text("entity_key", 64),
        _text("status", 20),
        _time("attempted_at"),
        _time("completed_at", True),
        _json("result_json"),
        sa.UniqueConstraint("tenant_id", "work_key"),
        sa.UniqueConstraint("tenant_id", "proposal_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "proposal_id"], ["transaction_ops_proposals.tenant_id", "transaction_ops_proposals.id"]
        ),
        sa.CheckConstraint("status IN ('executing','verified','unknown','failed')", name="ck_tx_operation_status"),
    )
    op.create_table(
        "transaction_ops_findings",
        *_common(),
        _uuid("run_id"),
        _text("order_reference", 100),
        _json("report_json"),
        sa.UniqueConstraint("tenant_id", "run_id", "order_reference"),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"]),
    )
    op.create_index(
        "uq_tx_proposal_active_work",
        "transaction_ops_proposals",
        ["tenant_id", "work_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','approved')"),
    )
    op.create_index(
        "uq_tx_operation_unsettled_entity",
        "transaction_ops_operations",
        ["tenant_id", "entity_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('executing','unknown')"),
    )
    for suffix in ("configs", "runs", "proposals", "operations", "findings"):
        table = f"transaction_ops_{suffix}"
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        if suffix in ("runs", "proposals", "operations"):
            op.create_index(f"ix_{table}_status", table, ["status"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = get_current_tenant_id()) "
            "WITH CHECK (tenant_id = get_current_tenant_id())"
        )

    op.execute("""
        CREATE FUNCTION transaction_ops_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE mutable text[];
        BEGIN
            IF TG_TABLE_NAME = 'transaction_ops_configs' THEN
                mutable := ARRAY['enabled','schedule_enabled','updated_at'];
            ELSIF TG_TABLE_NAME = 'transaction_ops_findings' THEN
                mutable := ARRAY['report_json','updated_at'];
            ELSIF TG_TABLE_NAME = 'transaction_ops_runs' THEN
                mutable := ARRAY['status','termination_reason','api_calls_used','orders_used',
                    'lease_token','lease_until','progress_json','finished_at','updated_at'];
                IF NEW.api_calls_used < OLD.api_calls_used OR NEW.orders_used < OLD.orders_used
                    OR (OLD.status = 'finished' AND
                        (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at')) THEN
                    RAISE EXCEPTION 'immutable run spend or terminal state';
                END IF;
            ELSIF TG_TABLE_NAME = 'transaction_ops_proposals' THEN
                mutable := ARRAY['status','decided_by','decided_at','decision_note','updated_at'];
                IF (OLD.status IN ('rejected','superseded') AND
                    (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at'))
                    OR (OLD.status = 'approved' AND NEW.status NOT IN ('approved','superseded'))
                    OR (OLD.status <> 'pending' AND ROW(NEW.decided_by,NEW.decided_at,NEW.decision_note)
                        IS DISTINCT FROM ROW(OLD.decided_by,OLD.decided_at,OLD.decision_note)) THEN
                    RAISE EXCEPTION 'immutable approval decision';
                END IF;
            ELSE
                mutable := ARRAY['status','completed_at','result_json','updated_at'];
                IF (OLD.status IN ('verified','failed') AND
                    (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at'))
                    OR (OLD.status = 'unknown' AND NEW.status = 'executing') THEN
                    RAISE EXCEPTION 'immutable operation attempt';
                END IF;
            END IF;
            IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
                RAISE EXCEPTION 'immutable transaction evidence';
            END IF;
            RETURN NEW;
        END $$
    """)
    for suffix in ("configs", "runs", "proposals", "operations", "findings"):
        table = f"transaction_ops_{suffix}"
        op.execute(
            f"CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION transaction_ops_guard()"
        )


def downgrade():
    for suffix in ("findings", "operations", "proposals", "runs", "configs"):
        op.drop_table(f"transaction_ops_{suffix}")
    op.execute("DROP FUNCTION transaction_ops_guard()")
