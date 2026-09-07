"""Start the bounded investigation clock on its first worker claim.

Revision ID: 104_tx_run_queue_time
Revises: 103_transaction_source_dates
"""

from alembic import op

revision = "104_tx_run_queue_time"
down_revision = "103_transaction_source_dates"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION transaction_ops_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            mutable text[] := ARRAY['status','termination_reason','api_calls_used','orders_used',
                'lease_token','lease_until','progress_json','finished_at','updated_at'];
            duration integer;
            hard_deadline timestamptz;
        BEGIN
            IF NEW.status NOT IN ('pending','running','finished')
                OR (OLD.status = 'running' AND NEW.status = 'pending') THEN
                RAISE EXCEPTION 'invalid run state transition';
            END IF;
            IF NEW.api_calls_used < OLD.api_calls_used OR NEW.orders_used < OLD.orders_used
                OR (OLD.status = 'finished' AND
                    (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at')) THEN
                RAISE EXCEPTION 'immutable run spend or terminal state';
            END IF;
            IF NEW.deadline_at IS DISTINCT FROM OLD.deadline_at THEN
                IF OLD.status <> 'pending' OR NEW.status <> 'running'
                    OR OLD.origin NOT IN ('manual','chat','schedule')
                    OR OLD.lease_token IS NOT NULL OR NEW.lease_token IS NULL
                    OR OLD.api_calls_used <> 0 OR NEW.api_calls_used <> 0
                    OR OLD.orders_used <> 0 OR NEW.orders_used <> 0 THEN
                    RAISE EXCEPTION 'immutable run execution deadline';
                END IF;
                duration := (OLD.config_snapshot->>'deadline_seconds')::integer;
                IF duration IS NULL OR duration NOT BETWEEN 30 AND 3600 THEN
                    RAISE EXCEPTION 'invalid first claim budget';
                END IF;
                hard_deadline := OLD.deadline_at - make_interval(secs => duration) + interval '24 hours';
                IF OLD.progress_json->>'continuation_started_at' IS NOT NULL THEN
                    hard_deadline := LEAST(hard_deadline,
                        (OLD.progress_json->>'continuation_started_at')::timestamptz + interval '24 hours');
                END IF;
                IF NEW.deadline_at <= clock_timestamp()
                    OR NEW.deadline_at > LEAST(hard_deadline, clock_timestamp() + make_interval(secs => duration)) THEN
                    RAISE EXCEPTION 'first claim exceeds bounded run budget';
                END IF;
                mutable := array_append(mutable, 'deadline_at');
            END IF;
            IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
                RAISE EXCEPTION 'immutable transaction evidence';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("DROP TRIGGER transaction_ops_immutable ON transaction_ops_runs")
    op.execute("""
        CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON transaction_ops_runs
        FOR EACH ROW EXECUTE FUNCTION transaction_ops_run_guard()
    """)


def downgrade():
    op.execute("DROP TRIGGER transaction_ops_immutable ON transaction_ops_runs")
    op.execute("""
        CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON transaction_ops_runs
        FOR EACH ROW EXECUTE FUNCTION transaction_ops_guard()
    """)
    op.execute("DROP FUNCTION transaction_ops_run_guard()")
