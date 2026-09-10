"""Allow one bounded first-claim clock for post-operation financial checks."""

from alembic import op

revision = "107_tx_settlement_queue"
down_revision = "106_transaction_cases"
branch_labels = None
depends_on = None

_BASE_GUARD = """
        CREATE OR REPLACE FUNCTION transaction_ops_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
    """


def upgrade():
    op.execute(
        _BASE_GUARD.replace(
            "OLD.origin NOT IN ('manual','chat','schedule')",
            "(OLD.origin NOT IN ('manual','chat','schedule') AND NOT (OLD.origin = 'recovery' "
            "AND (OLD.params_json->>'verification_scope') IS NOT DISTINCT FROM 'order_total_tax_refunds'))",
        )
    )


def downgrade():
    op.execute(_BASE_GUARD)
