"""celigo_flow_steps: NetSuite provenance columns -- Task 7 fix round 2

Task 11 derives "which flows write which NetSuite record types" from
`netsuite_da.recordType`/`operation` (imports) and `netsuite.restlet.
recordType`/`searchId` (exports) -- fields Phase D (`sync_service.py`)
already fetches (`repository.backfill_flow_step_reference_info`) but had
nowhere durable to land. Team-lead-flagged data dependency: Task 11 is
blocked without this.

DESIGN CALL (raw_json vs dedicated columns -- asked for explicitly, decided
here): dedicated columns, not `celigo_flow_steps.raw_json`. Two reasons:
  1. `adaptor_type`/`connection_celigo_id` -- the SAME category of data
     (provenance fetched from the referenced export/import object, backfilled
     once per sync) -- are ALREADY dedicated columns on this exact table
     (fix round 1). Splitting "some provenance fields are real columns,
     other closely-related provenance fields are buried in JSON" is an
     inconsistent schema for no benefit.
  2. `celigo_flow_steps.raw_json` already has a DOCUMENTED meaning from
     migration 094's own docstring (deviation 7): "the full sanitized
     object" -- of the STEP's own payload (the pageProcessor/pageGenerator
     entry, from the FLOW's sanitized dict). Stuffing a DIFFERENT object's
     data (the referenced export/import's provenance) into that column would
     silently redefine what it holds, confusing any future reader who trusts
     that docstring. Dedicated columns keep the two concerns separate.
  Task 11 also needs to QUERY on `record_type` (its whole point is grouping
  flows by NetSuite record type) -- a plain column is the simpler target for
  that than a JSONB path expression, even though this migration adds no
  index for it (see below).

ALTER TABLE only -- `celigo_flow_steps` already exists (migration 094),
already ENABLE+FORCE RLS'd. Adding a column to an already-RLS'd table
inherits the existing table-scoped policy automatically; RLS in Postgres is
not column-scoped, so no RLS statements belong in this migration.

`record_type` is SHARED across both kinds -- `netsuite_da.recordType`
(imports) and `netsuite.restlet.recordType` (exports) are the same semantic
field under different parent keys (observed-shapes.md: "imports carry
netsuite_da; exports carry netsuite -- DIFFERENT KEY FROM IMPORTS"), so one
column serves both. `operation` is import-only (exports carry no equivalent
field in the observed shape); `search_id` is export-only (`netsuite.restlet.
searchId`; imports carry no equivalent). A step backfilled from the "wrong"
kind for a given column is expected to leave it NULL -- never guessed at.

No index added here, deliberately: Task 11 (not built yet) is the actual
consumer and should add whatever index its real query shape needs (plain
`record_type`, a composite with `operation`, a partial index scoped to
non-null) once that shape is known, rather than this migration guessing at
one now with no query to validate it against.

Parents on `095_celigo_config_changes` (current single head, verified via
`alembic heads` against the scratch DB).

Revision ID: 096_celigo_flow_step_provenance
Revises: 095_celigo_config_changes
Create Date: 2026-08-27
"""

import sqlalchemy as sa

from alembic import op

revision = "096_celigo_flow_step_provenance"
down_revision = "095_celigo_config_changes"
branch_labels = None
depends_on = None

_TABLE = "celigo_flow_steps"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("record_type", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("operation", sa.Text(), nullable=True))  # import-only
    op.add_column(_TABLE, sa.Column("search_id", sa.Text(), nullable=True))  # export-only


def downgrade() -> None:
    op.drop_column(_TABLE, "search_id")
    op.drop_column(_TABLE, "operation")
    op.drop_column(_TABLE, "record_type")
