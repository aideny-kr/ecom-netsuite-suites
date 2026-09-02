"""write_side_effects: record every external write BEFORE it is sent

The table that makes "we sent it and do not know" representable.

Today a NetSuite write has two recordable outcomes and a third that actually
happens: the request was sent and no answer came back. On 2026-08-27 that case
created sandbox customer 5264348 while the application reported the write as
`failed` and offered to run the identical payload again. PR #210 taught the CARD
to say "indeterminate"; it gave the system nowhere to record that a call was in
flight when the process died, and no way to settle it afterwards.

`.claude/rules/agent-graph.md` #10 requires "a work-derived idempotency key and a
side-effect log written *before* the call, so a crash between send and confirm is
recoverable", and records that none of it was built. This is that log.

DESIGN CALLS, made here rather than left to the reader:

1. A NEW TABLE, not columns on `chat_messages`. The card is a conversational
   artifact; this is a ledger of external side effects. A batch write has no
   single message to hang N rows off, and a resume path must be able to query
   "what is unsettled for this tenant" without walking chat history.

2. `UNIQUE (tenant_id, idempotency_key)`. Two attempts at the same work share one
   row and update it. Appending a second row would make the log imply two writes
   where there was one — the log lying about side effects is worse than no log.

3. `status` DEFAULTS TO 'attempted' IN THE DATABASE, not just in the model. The
   row is inserted before the call precisely because the process may die
   immediately after; a default that lives only in Python is a default that is
   absent exactly when it matters.

4. NO FK on `connector_id`/`session_id`. A side-effect record must outlive the
   connector it was sent through — a disconnected NetSuite account is when you
   most need to know what was already written to it. Deleting a connector must
   not cascade away the evidence.

5. Nullable `batch_id`/`row_index`. Phase 1 ships ALONE, before any batch UI, and
   must make the single-record timeout answerable. A batchless write still gets
   a row.

Reversible: `downgrade` drops the table whole. Nothing else references it yet.

Create Date: 2026-09-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "097_write_side_effects"
# The true head is `094_dashboard_preference_series`, NOT the highest number:
# 096 -> 093_report_series -> 094_dashboard_preference_series. Parenting on 096
# (which already has a child) forked the graph into two heads. RE-PARENTED, not
# merged — a merge migration makes `downgrade -1` ambiguous and is banned here.
down_revision = "094_dashboard_preference_series"  # current single head (verify: alembic heads)
branch_labels = None
depends_on = None

_TABLE = "write_side_effects"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("record_type", sa.String(80), nullable=False),
        sa.Column("mutation_type", sa.String(20), nullable=False),
        # Server-side default: the row is written before the call, so the safe
        # state must be guaranteed by the database, not by the caller remembering.
        sa.Column("status", sa.String(20), nullable=False, server_default="attempted"),
        sa.Column("netsuite_record_id", sa.String(64), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_write_side_effect_tenant_key"),
    )
    op.create_index("ix_write_side_effects_tenant_id", _TABLE, ["tenant_id"])
    op.create_index("ix_write_side_effects_status", _TABLE, ["status"])
    op.create_index("ix_write_side_effects_batch_id", _TABLE, ["batch_id"])
    op.create_index("ix_write_side_effects_correlation_id", _TABLE, ["correlation_id"])
    op.create_index("ix_write_side_effects_connector_id", _TABLE, ["connector_id"])
    # The resume query: "what is still in flight for this tenant?"
    op.create_index("ix_write_side_effect_unsettled", _TABLE, ["tenant_id", "status"])

    # Tenant isolation, matching every other tenant-scoped table here.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_TABLE}_tenant_isolation ON {_TABLE}
        USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        """
    )
    # ENABLE alone does NOT constrain the table's OWNER, and on Supabase the
    # application connects as the owner — so without FORCE the policy above is
    # decorative and every tenant's rows are readable. Migrations 087/089/092/
    # 093/094/095 all pair the two for this reason; this table holds write
    # payloads, so it is not the one to break the pattern on.
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")  # load-bearing on Supabase (owner != BYPASSRLS)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_tenant_isolation ON {_TABLE}")
    op.drop_table(_TABLE)
