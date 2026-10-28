"""A person may resolve a transaction case after a fresh check (decided by Aiden, 2026-10-27/28).

Adds the case status 'resolved' and `resolution_json` (who, when, why, the remaining difference they
accepted, and any reopening). The case guard from 106/117 keeps identity immutable and a reconciled case
final; it now also lets `resolution_json` change. 'resolved' may move to 'open' (the difference changed,
or a person reopened it) or to 'reconciled' (the order later matched).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "118_case_resolved_by_person"
down_revision = "117_reconciled_case_final"
branch_labels = None
depends_on = None

_GUARD = """CREATE OR REPLACE FUNCTION transaction_case_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_TABLE_NAME = 'transaction_case_observations' OR TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'immutable transaction case observation';
        END IF;
        IF (to_jsonb(NEW) - ARRAY[{mutable}]) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY[{mutable}]) OR
           NEW.last_observed_at < OLD.last_observed_at THEN
            RAISE EXCEPTION 'immutable transaction case identity';
        END IF;
        IF OLD.status = 'reconciled' AND NEW.status IS DISTINCT FROM 'reconciled' THEN
            RAISE EXCEPTION 'reconciled_case_is_final';
        END IF;
        RETURN NEW;
    END $$"""

_MUTABLE_BEFORE = "'status','last_observed_at','latest_report_json','updated_at'"
_MUTABLE_AFTER = "'status','last_observed_at','latest_report_json','updated_at','resolution_json'"


def upgrade():
    op.add_column("transaction_cases", sa.Column("resolution_json", JSONB, nullable=True))
    op.drop_constraint("ck_transaction_case_status", "transaction_cases", type_="check")
    op.create_check_constraint(
        "ck_transaction_case_status", "transaction_cases", "status IN ('open','reconciled','resolved')"
    )
    op.execute(_GUARD.format(mutable=_MUTABLE_AFTER))


def downgrade():
    op.execute(_GUARD.format(mutable=_MUTABLE_BEFORE))
    # A resolved case goes back to open: the older schema has no other place for it.
    op.execute("UPDATE transaction_cases SET status = 'open' WHERE status = 'resolved'")
    op.drop_constraint("ck_transaction_case_status", "transaction_cases", type_="check")
    op.create_check_constraint("ck_transaction_case_status", "transaction_cases", "status IN ('open','reconciled')")
    op.drop_column("transaction_cases", "resolution_json")
