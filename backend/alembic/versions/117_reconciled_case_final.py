"""A reconciled transaction case can never be reopened.

Extends the case guard from 106: an update that moves a case out of 'reconciled' is refused, so no
writer (scan, batch or a future path) can reopen one. A later matching scan may still refresh a
reconciled case's evidence; a real change after reconciliation is recorded as an audit event.
"""

from alembic import op

revision = "117_reconciled_case_final"
down_revision = "116_policy_replays"
branch_labels = None
depends_on = None

_GUARD = """CREATE OR REPLACE FUNCTION transaction_case_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_TABLE_NAME = 'transaction_case_observations' OR TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'immutable transaction case observation';
        END IF;
        IF (to_jsonb(NEW) - ARRAY['status','last_observed_at','latest_report_json','updated_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['status','last_observed_at','latest_report_json','updated_at']) OR
           NEW.last_observed_at < OLD.last_observed_at THEN
            RAISE EXCEPTION 'immutable transaction case identity';
        END IF;{final}
        RETURN NEW;
    END $$"""

_FINAL = """
        IF OLD.status = 'reconciled' AND NEW.status IS DISTINCT FROM 'reconciled' THEN
            RAISE EXCEPTION 'reconciled_case_is_final';
        END IF;"""


def upgrade():
    op.execute(_GUARD.format(final=_FINAL))


def downgrade():
    op.execute(_GUARD.format(final=""))
