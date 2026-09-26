"""Keep compact review facts synchronized with authoritative finding reports.

The nullable column is additive and needs no table rewrite. Existing findings
keep the original read path until the bounded, tenant-scoped backfill runs.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "115_review_read_projection"
down_revision = "114_dependency_batches"
branch_labels = None
depends_on = None

PROJECT_FUNCTION = """
CREATE FUNCTION public.transaction_review_metadata(doc jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog
AS $$
  SELECT CASE WHEN jsonb_typeof(doc) <> 'object' THEN doc ELSE
    jsonb_build_object(
      'source', jsonb_build_object(
        'record_id', doc #> '{source,record_id}',
        'observed_at', doc #> '{source,observed_at}'),
      'targets', CASE WHEN jsonb_typeof(doc -> 'targets') = 'array' THEN
        COALESCE((
          SELECT jsonb_agg(jsonb_build_object('observed_at', item -> 'observed_at') ORDER BY position)
          FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(doc -> 'targets') = 'array' THEN doc -> 'targets' ELSE '[]'::jsonb END
          ) WITH ORDINALITY AS targets(item, position)
        ), '[]'::jsonb)
        ELSE 'null'::jsonb END,
      '_observation', jsonb_build_object(
        'observed_at', doc #> '{_observation,observed_at}',
        'final', doc #> '{_observation,final}'),
      'balance', jsonb_build_object(
        'currency', doc #> '{balance,currency}',
        'status', doc #> '{balance,status}'),
      'source_eligibility', jsonb_build_object('eligible', doc #> '{source_eligibility,eligible}')
    ) END
$$
"""


def upgrade():
    op.add_column("transaction_ops_findings", sa.Column("review_metadata_json", JSONB(), nullable=True))
    op.execute(PROJECT_FUNCTION)
    # Database maintenance covers old worker images, ORM writes and bulk upserts
    # in the same transaction. Callers cannot forge the derived projection.
    op.execute("""
        CREATE FUNCTION public.sync_transaction_review_metadata()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          NEW.review_metadata_json := public.transaction_review_metadata(NEW.report_json);
          RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER sync_transaction_review_metadata
        BEFORE INSERT OR UPDATE OF report_json, review_metadata_json ON transaction_ops_findings
        FOR EACH ROW EXECUTE FUNCTION public.sync_transaction_review_metadata()
    """)

    # Keep the existing immutable identity/evidence contract while allowing the
    # database-maintained projection to follow report updates and backfills.
    op.execute("""
        CREATE FUNCTION public.transaction_finding_guard()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY['report_json','review_metadata_json','updated_at'])
             IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY['report_json','review_metadata_json','updated_at']) THEN
            RAISE EXCEPTION 'immutable transaction evidence';
          END IF;
          RETURN NEW;
        END
        $$
    """)
    op.execute("DROP TRIGGER transaction_ops_immutable ON transaction_ops_findings")
    op.execute("""
        CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON transaction_ops_findings
        FOR EACH ROW EXECUTE FUNCTION public.transaction_finding_guard()
    """)


def downgrade():
    op.execute("DROP TRIGGER transaction_ops_immutable ON transaction_ops_findings")
    op.execute("""
        CREATE TRIGGER transaction_ops_immutable BEFORE UPDATE ON transaction_ops_findings
        FOR EACH ROW EXECUTE FUNCTION transaction_ops_guard()
    """)
    op.execute("DROP FUNCTION public.transaction_finding_guard()")
    op.execute("DROP TRIGGER sync_transaction_review_metadata ON transaction_ops_findings")
    op.execute("DROP FUNCTION public.sync_transaction_review_metadata()")
    op.drop_column("transaction_ops_findings", "review_metadata_json")
    op.execute("DROP FUNCTION public.transaction_review_metadata(jsonb)")
