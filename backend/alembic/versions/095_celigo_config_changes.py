"""celigo_config_changes -- Task 7 drift-detection audit table, ENABLE + FORCE RLS

Parents onto `094_celigo_flow_map` (the current single head on this branch,
verified via `alembic heads` against the scratch DB). Per
`092_user_dashboard_preference.py`'s template -- copied exactly, same as
migration 094 did: `ENABLE ROW LEVEL SECURITY`, `CREATE POLICY` with
`get_current_tenant_id()` in both `USING` and `WITH CHECK`, then `FORCE ROW
LEVEL SECURITY` -- "load-bearing on Supabase (table owner is not BYPASSRLS)".

Applied to the LOCAL SCRATCH DB ONLY (human ruling, binding on this task) --
never Supabase, never the shared `ecom_netsuite` DB.

⚠️ Another unmerged branch also claims migration number `094` (see
`094_celigo_flow_map`'s own docstring). If that branch lands on `main` first,
THIS migration (095, chained off `094_celigo_flow_map`) must be RE-PARENTED
alongside it -- renumbered and re-chained, never resolved with a merge
migration (`093_recon_reject_labels`'s own docstring explains why a merge
head breaks `downgrade -1`).

One row per detected field-level drift between two syncs (design spec §4.5):
"Each sync diffs the incoming object against the stored one on a small
watched set -- disabled, schedule, import mapping_json, export filter_json,
script content_hash -- and appends to celigo_config_changes." Append-only,
like `celigo_flow_errors` -- a row here is a historical fact ("this field
changed at this time"), never updated or deleted by anything in this branch.

DESIGN: polymorphic over three object kinds (`object_kind`, CHECK'd to
'flow' | 'flow_step' | 'script') rather than three separate tables -- the
watched set itself spans three different tables (`celigo_flows.disabled`/
`schedule`, `celigo_flow_steps.mapping_json`/`filter_json`,
`celigo_scripts.content_hash`), and a drift-history feed (out of scope here)
wants one table to read, not three to union. `object_id` has NO foreign key
-- a single column can't reference three different parent tables -- see
`app/models/celigo.py`'s `CeligoConfigChange` docstring for why an orphaned
`object_id` is safe (the parent config-mirror rows are CASCADE-deleted with
their connection anyway). `flow_id` (nullable, real FK to `celigo_flows`,
CASCADE) IS populated for 'flow' and 'flow_step' kinds -- both always belong
to exactly one flow -- and left NULL for 'script' kind, since a script is not
owned by any single flow (it can be attached from many, or none synced yet).
`celigo_id` is the raw Celigo id of the object that drifted, denormalized off
`object_id` on purpose: it survives even if the parent row is later deleted,
unlike `object_id`, which carries no FK of its own.

`old_value`/`new_value` are JSONB, not typed per field, because the watched
set spans four Python types across five fields (`disabled` is bool;
`schedule`/`mapping_json`/`filter_json` are dicts; `content_hash` is a
string) -- JSONB stores any of them without a lossy string cast.

CASCADE, not SET NULL, on both `celigo_connection_id` and `flow_id`: unlike
`celigo_flow_errors`/`celigo_error_signatures` (the audit trail that exists
specifically to outlive Celigo's ~30-day purge), a config-drift row is
meaningless without the connection and flow it describes -- there is no
external deletion deadline motivating this table to survive its parent's
deletion. Matches the CASCADE five in migration 094's own CASCADE-vs-SET-NULL
split, not the SET NULL two.

Revision ID: 095_celigo_config_changes
Revises: 094_celigo_flow_map
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "095_celigo_config_changes"
down_revision = "094_celigo_flow_map"
branch_labels = None
depends_on = None

_TABLE = "celigo_config_changes"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "celigo_connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("flow_id", UUID(as_uuid=True), sa.ForeignKey("celigo_flows.id", ondelete="CASCADE"), nullable=True),
        sa.Column("object_kind", sa.Text(), nullable=False),  # 'flow' | 'flow_step' | 'script'
        sa.Column("object_id", UUID(as_uuid=True), nullable=True),  # polymorphic -- no FK, see module docstring
        sa.Column("celigo_id", sa.Text(), nullable=False),  # raw Celigo id of the object that drifted
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("old_value", JSONB(), nullable=True),
        sa.Column("new_value", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "object_kind IN ('flow', 'flow_step', 'script')", name="ck_celigo_config_changes_object_kind"
        ),
        sa.CheckConstraint(
            "field IN ('disabled', 'schedule', 'mapping_json', 'filter_json', 'content_hash')",
            name="ck_celigo_config_changes_field",
        ),
    )
    op.create_index("ix_celigo_config_changes_tenant_id", _TABLE, ["tenant_id"])
    op.create_index("ix_celigo_config_changes_connection_id", _TABLE, ["celigo_connection_id"])
    op.create_index("ix_celigo_config_changes_flow_id", _TABLE, ["flow_id"])
    op.create_index("ix_celigo_config_changes_tenant_object", _TABLE, ["tenant_id", "object_kind", "object_id"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {_TABLE}_tenant_isolation ON {_TABLE}
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")  # load-bearing on Supabase (owner != BYPASSRLS)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_tenant_isolation ON {_TABLE}")
    op.drop_table(_TABLE)
