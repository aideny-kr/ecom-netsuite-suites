"""Hold a read's worst case apart from what it actually spent.

A run reserves the most provider calls a read could need before making it, so a crash
mid-read can never leave calls unaccounted. Until now the reservation *was* the spend:
``api_calls_used`` carried the worst case whether or not it was used, and the guard below
made it append-only, so the unused part could never come back. Framework Inc's scan paid
the full 28-call refund reservation on every order, most of which have no refunds, and got
through about 45 orders per 2,000-call run.

``api_calls_held`` now carries reservations. A read settles its hold into ``api_calls_used``
at what it actually sent and drops the rest. ``api_calls_used`` stays append-only, and the
ceiling covers both, so a held call still cannot be exceeded. Anything still held when a
run finishes is folded into spend, which is exactly the old behaviour for a crash or an
unsettled read: assume the worst case was spent. A finished run may hold nothing.
"""

import sqlalchemy as sa

from alembic import op

revision = "110_run_call_holds"
down_revision = "109_write_kernel_operations"
branch_labels = None
depends_on = None

# The guard as migration 107 left it, recovery clause included, parameterised by the
# pieces this migration changes.
_GUARD = """
        CREATE OR REPLACE FUNCTION transaction_ops_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            mutable text[] := ARRAY['status','termination_reason','api_calls_used',{held_mutable}'orders_used',
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
            END IF;{held_rules}
            IF NEW.deadline_at IS DISTINCT FROM OLD.deadline_at THEN
                IF OLD.status <> 'pending' OR NEW.status <> 'running'
                    OR (OLD.origin NOT IN ('manual','chat','schedule') AND NOT (OLD.origin = 'recovery'
                        AND (OLD.params_json->>'verification_scope') IS NOT DISTINCT FROM 'order_total_tax_refunds'))
                    OR OLD.lease_token IS NOT NULL OR NEW.lease_token IS NULL
                    OR OLD.api_calls_used <> 0 OR NEW.api_calls_used <> 0{held_first_claim}
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

_WITH_HOLDS = _GUARD.format(
    held_mutable="'api_calls_held',",
    held_rules="""
            IF NEW.status = 'finished' AND NEW.api_calls_held <> 0 THEN
                RAISE EXCEPTION 'finished run still holds reserved calls';
            END IF;""",
    held_first_claim="\n                    OR OLD.api_calls_held <> 0 OR NEW.api_calls_held <> 0",
)
_WITHOUT_HOLDS = _GUARD.format(held_mutable="", held_rules="", held_first_claim="")

_SPEND_WITH_HOLDS = (
    "api_calls_used >= 0 AND api_calls_held >= 0 AND api_calls_used + api_calls_held <= max_api_calls "
    "AND orders_used >= 0 AND orders_used <= max_orders"
)
_SPEND_WITHOUT_HOLDS = (
    "api_calls_used >= 0 AND api_calls_used <= max_api_calls AND orders_used >= 0 AND orders_used <= max_orders"
)


def upgrade():
    op.add_column(
        "transaction_ops_runs",
        sa.Column("api_calls_held", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(_WITH_HOLDS)
    op.drop_constraint("ck_tx_run_spend", "transaction_ops_runs", type_="check")
    op.create_check_constraint("ck_tx_run_spend", "transaction_ops_runs", _SPEND_WITH_HOLDS)


def downgrade():
    # Fold any open hold into spend first, while the guard still permits holds to move.
    op.execute(
        "UPDATE transaction_ops_runs SET api_calls_used = api_calls_used + api_calls_held, "
        "api_calls_held = 0 WHERE api_calls_held <> 0"
    )
    op.drop_constraint("ck_tx_run_spend", "transaction_ops_runs", type_="check")
    op.create_check_constraint("ck_tx_run_spend", "transaction_ops_runs", _SPEND_WITHOUT_HOLDS)
    op.execute(_WITHOUT_HOLDS)
    op.drop_column("transaction_ops_runs", "api_calls_held")
