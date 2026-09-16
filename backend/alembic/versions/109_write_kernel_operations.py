"""Write kernel: approval sources, outcome taxonomy and lineage on the operation ledger.

Revision ID: 109_write_kernel_operations
Revises: 108_chat_submissions

The operation ledger becomes the only way any surface sends a mutation
(docs/superpowers/specs/2026-09-15-write-kernel-design.md). This revision gives it:

* an approval source (``approval_kind`` + ``approval_id``) so a chat confirmation can be
  claimed the same way a transaction proposal is; ``proposal_id`` becomes nullable;
* the surface, provider and adapter that made the attempt;
* lineage (``base_work_key`` + ``retry_of_operation_id``) so a corrected resubmit keeps the
  business identity it retries instead of evading the duplicate check;
* the outcome taxonomy ``executing | rejected_before_effect | committed_unverified | unknown
  | verified | needs_review``. Every existing ``failed`` row is pre-effect by construction
  (an exception after the permit always ends ``unknown``) and is renamed. ``failed`` stays
  accepted by the CHECK for one release so branches that still write it keep working
  against a shared database; the service no longer writes it.

The guard trigger from 100 is recreated with the wider terminal set; the new columns are
frozen by it automatically because they are not in its mutable list.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "109_write_kernel_operations"
down_revision = "108_chat_submissions"
branch_labels = None
depends_on = None

TABLE = "transaction_ops_operations"
STATUSES = "('executing','rejected_before_effect','committed_unverified','unknown','verified','needs_review','failed')"
LEGACY_STATUSES = "('executing','verified','unknown','failed')"
TRIGGER = (
    f"CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON {TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION transaction_ops_operation_guard()"
)
# Legacy writers (the previous release during a rolling deploy, branches not yet carrying
# this revision) insert a proposal-approved row without the new columns. Fill them from the
# proposal the way the service would, so the new NOT NULL columns never break an old writer.
DEFAULTS_FUNCTION = """
    CREATE OR REPLACE FUNCTION transaction_ops_operation_defaults() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.approval_id IS NULL AND NEW.proposal_id IS NOT NULL THEN
            NEW.approval_id := NEW.proposal_id;
        END IF;
        IF NEW.base_work_key IS NULL THEN
            NEW.base_work_key := NEW.work_key;
        END IF;
        RETURN NEW;
    END $$
"""
DEFAULTS_TRIGGER = (
    f"CREATE TRIGGER transaction_ops_operation_defaults BEFORE INSERT ON {TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION transaction_ops_operation_defaults()"
)


def _guard(terminal: str, settled: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION transaction_ops_operation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE mutable text[] := ARRAY['status','completed_at','result_json','updated_at','api_calls_used'];
        BEGIN
            IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable)
                OR NEW.api_calls_used < OLD.api_calls_used THEN
                RAISE EXCEPTION 'immutable operation budget or evidence';
            END IF;
            IF (OLD.status IN {terminal} AND
                (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at'))
                OR (OLD.status IN {settled} AND NEW.status = 'executing') THEN
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
    """


def upgrade():
    op.execute(f"DROP TRIGGER transaction_ops_immutable ON {TABLE}")
    op.alter_column(TABLE, "status", type_=sa.String(32), existing_type=sa.String(20))  # rejected_before_effect
    op.alter_column(TABLE, "proposal_id", nullable=True)
    op.add_column(
        TABLE, sa.Column("approval_kind", sa.String(32), server_default="transaction_proposal", nullable=False)
    )
    op.add_column(TABLE, sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(TABLE, sa.Column("surface", sa.String(16), server_default="scheduled", nullable=False))
    op.add_column(TABLE, sa.Column("provider", sa.String(32), nullable=True))
    op.add_column(TABLE, sa.Column("adapter", sa.String(64), nullable=True))
    op.add_column(TABLE, sa.Column("base_work_key", sa.String(64), nullable=True))
    op.add_column(TABLE, sa.Column("retry_of_operation_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(
        f"UPDATE {TABLE} SET approval_id = proposal_id, base_work_key = work_key, "
        "provider = COALESCE(result_json->>'provider', provider)"
    )
    op.alter_column(TABLE, "approval_id", nullable=False)
    op.alter_column(TABLE, "base_work_key", nullable=False)
    op.execute(f"UPDATE {TABLE} SET status = 'rejected_before_effect' WHERE status = 'failed'")
    op.drop_constraint(f"{TABLE}_tenant_id_proposal_id_key", TABLE, type_="unique")
    op.create_unique_constraint("uq_tx_operation_approval", TABLE, ["tenant_id", "approval_kind", "approval_id"])
    # The sibling transaction_ops tables carry (tenant_id, id); the ledger did not, and the
    # tenant-scoped lineage foreign key needs it.
    op.create_unique_constraint(f"{TABLE}_tenant_id_id_key", TABLE, ["tenant_id", "id"])
    op.create_foreign_key(
        "fk_tx_operation_retry_of", TABLE, TABLE, ["tenant_id", "retry_of_operation_id"], ["tenant_id", "id"]
    )
    op.drop_constraint("ck_tx_operation_status", TABLE, type_="check")
    op.create_check_constraint("ck_tx_operation_status", TABLE, f"status IN {STATUSES}")
    op.create_check_constraint(
        "ck_tx_operation_approval_kind", TABLE, "approval_kind IN ('transaction_proposal','chat_confirmation')"
    )
    op.create_check_constraint("ck_tx_operation_surface", TABLE, "surface IN ('chat','scheduled','group','backfill')")
    op.create_check_constraint(
        "ck_tx_operation_proposal_approval",
        TABLE,
        "approval_kind <> 'transaction_proposal' OR (proposal_id IS NOT NULL AND approval_id = proposal_id)",
    )
    op.drop_index("uq_tx_operation_unsettled_entity", table_name=TABLE)
    op.create_index(
        "uq_tx_operation_unsettled_entity",
        TABLE,
        ["tenant_id", "entity_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('executing','unknown','committed_unverified')"),
    )
    op.execute(
        _guard(
            terminal="('verified','rejected_before_effect','needs_review','failed')",
            settled="('unknown','committed_unverified')",
        )
    )
    op.execute(TRIGGER)
    op.execute(DEFAULTS_FUNCTION)  # one statement per execute: asyncpg prepares each
    op.execute(DEFAULTS_TRIGGER)


def downgrade():
    op.execute(f"DROP TRIGGER IF EXISTS transaction_ops_operation_defaults ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS transaction_ops_operation_defaults()")
    op.execute(f"DROP TRIGGER transaction_ops_immutable ON {TABLE}")
    # Fold the wider taxonomy back into the four legacy values without ever making a
    # sent-but-unproven attempt look final: unknown blocks a resend, failed does not.
    op.execute(f"UPDATE {TABLE} SET status = 'unknown' WHERE status IN ('committed_unverified','needs_review')")
    op.execute(f"UPDATE {TABLE} SET status = 'failed' WHERE status = 'rejected_before_effect'")
    op.drop_index("uq_tx_operation_unsettled_entity", table_name=TABLE)
    op.create_index(
        "uq_tx_operation_unsettled_entity",
        TABLE,
        ["tenant_id", "entity_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('executing','unknown')"),
    )
    for name in ("ck_tx_operation_proposal_approval", "ck_tx_operation_surface", "ck_tx_operation_approval_kind"):
        op.drop_constraint(name, TABLE, type_="check")
    op.drop_constraint("ck_tx_operation_status", TABLE, type_="check")
    op.create_check_constraint("ck_tx_operation_status", TABLE, f"status IN {LEGACY_STATUSES}")
    op.drop_constraint("fk_tx_operation_retry_of", TABLE, type_="foreignkey")
    op.drop_constraint(f"{TABLE}_tenant_id_id_key", TABLE, type_="unique")
    op.drop_constraint("uq_tx_operation_approval", TABLE, type_="unique")
    op.execute(f"DELETE FROM {TABLE} WHERE proposal_id IS NULL")  # rows no legacy reader can address
    op.create_unique_constraint(f"{TABLE}_tenant_id_proposal_id_key", TABLE, ["tenant_id", "proposal_id"])
    for column in (
        "retry_of_operation_id",
        "base_work_key",
        "adapter",
        "provider",
        "surface",
        "approval_id",
        "approval_kind",
    ):
        op.drop_column(TABLE, column)
    op.alter_column(TABLE, "proposal_id", nullable=False)
    op.alter_column(TABLE, "status", type_=sa.String(20), existing_type=sa.String(32))
    op.execute(_guard(terminal="('verified','failed')", settled="('unknown')"))
    op.execute(TRIGGER)
