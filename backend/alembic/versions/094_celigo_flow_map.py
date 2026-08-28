"""celigo flow map — seven tables (integrations/flows/steps/scripts/
attachments/error signatures/errors), all ENABLE + FORCE row-level security

Per the `092_user_dashboard_preference.py` template: ENABLE AND FORCE ROW
LEVEL SECURITY, `get_current_tenant_id()` in both USING and WITH CHECK. FORCE
is a DELIBERATE tightening beyond this repo's usual default (`connections` /
`mcp_connectors` are RLS-enabled but not forced, because the worker role that
reads them is BYPASSRLS) -- these seven tables carry synced Celigo config and
PII-bearing error text, so they get the `reports`/`metric_definitions`-style
hardened posture instead. "FORCE is load-bearing on Supabase (table owner is
not BYPASSRLS)", same as 084/092.

`093_recon_reject_labels` is the current single head on this branch (verified
via `alembic heads` against the scratch DB below), so 094 chains off 093.
`094` is ALSO used by another, unmerged branch's migration -- if that one
lands on origin/main first, THIS migration must be RE-PARENTED (renumbered
and re-chained) onto it once merged, never resolved with a merge migration.
093's own docstring explains why: a merge head breaks `downgrade -1`.

## Design deviations from the task brief -- each one earned by a live probe
(observed-shapes.md) or a real Postgres constraint, not a preference. Every
deviation is called out in task-4-report.md too.

1. **Not every table gets the brief's literal `(tenant_id,
   celigo_connection_id, celigo_id)` unique constraint.**
   - `celigo_flow_steps`: a `celigo_id` here is a *referenced* export/import
     id (`_exportId`/`_importId`), not a row this table owns the identity of
     -- the SAME export can be referenced from more than one flow, or more
     than one branch within one flow (observed-shapes.md: routers chain
     branches, and multi-subsidiary sales-order flows put most of their
     steps inside `routers[].branches[].pageProcessors`, not the top-level
     arrays). The 3-tuple alone can collide across flows/branches. Actual
     key: `(tenant_id, celigo_connection_id, flow_id, celigo_id,
     branch_key)` -- see deviation 2 for `branch_key`.
   - `celigo_script_attachments`: not a Celigo object at all -- it is a
     graph.py `ScriptRef` occurrence (script attach site), which has no
     Celigo-native id. Its natural key is `(tenant_id, flow_id, json_path)`:
     `json_path` is computed by `walk_script_refs` relative to the flow
     object root, so it is unique per occurrence within one flow by
     construction, whether or not the occurrence falls inside a step.
   - `celigo_error_signatures`: not a raw Celigo object either -- a locally
     computed fingerprint. Key: `(tenant_id, celigo_connection_id,
     fingerprint)`.
   All other four tables (`celigo_integrations`, `celigo_flows`,
   `celigo_scripts`, `celigo_flow_errors`) use the brief's literal 3-tuple:
   each row there DOES mirror exactly one Celigo object with an `_id`
   (`errorId` for errors) that's unique on its own.

2. **`branch_key` (celigo_flow_steps) is a STORED GENERATED column,
   `COALESCE(branch_id, '$root')`, not the raw nullable `branch_id`.**
   Postgres treats every NULL as DISTINCT for UNIQUE-constraint purposes, so
   a raw nullable `branch_id` in the unique tuple would make EVERY top-level
   (non-router) step "distinct" from every other top-level step regardless
   of `celigo_id`/`flow_id` matching -- a resync's `ON CONFLICT` would never
   fire for top-level steps and every sync would insert duplicates instead
   of upserting. `'$root'` is a safe sentinel: Celigo's own branch ids are
   short alphanumeric strings (e.g. `QApFJpruReZ`), never the literal
   `$root`.

3. **`dedup_key` (celigo_scripts) is the same fix, same reason, for script
   clone lineage.** observed-shapes.md, live-confirmed: a script CLONE
   carries `_sourceId` pointing at the original; the ORIGINAL has NO
   `_sourceId` (NULL). A future dedup query grouping scripts by raw
   `source_id` would put every source-less ORIGINAL into the SAME NULL
   group (GROUP BY treats NULLs as equal to each other, unlike UNIQUE) --
   merging unrelated originals' lineages together, while clones correctly
   group under their real source id. `dedup_key = COALESCE(source_id,
   celigo_id)` gives every original its own key (its own id) and every
   clone the same key as its original, structurally, so no future dedup
   query has to remember to `COALESCE` by hand. Indexed, not a uniqueness
   constraint -- clones legitimately share a `dedup_key` with their
   original and with each other.

4. **`celigo_script_attachments` has BOTH `flow_id` (NOT NULL) and
   `flow_step_id` (nullable), not just the brief's `flow_step_id`.**
   observed-shapes.md: `routers[].script` is a real, confirmed attachment
   site (graph.py's recursive walker finds it), but it belongs to the
   ROUTER, not to any one step/branch -- there is no `celigo_flow_steps` row
   to point it at. Keying attachments strictly to `flow_step_id` would make
   a router-level script ref unrepresentable, silently dropping exactly the
   kind of ref Task 2's walker exists to find. `flow_id` anchors every
   attachment regardless of where in the flow it was found; `flow_step_id`
   is populated when the ref falls inside a specific step's subtree and
   left NULL for router-level (or any other non-step-scoped) refs.
   `script_id` is nullable FK + a NOT NULL `script_celigo_id` raw string,
   for the same reason: script sync can lag flow sync, and the raw
   `_scriptId` the walker found must survive being stored even before the
   corresponding `celigo_scripts` row exists locally.

5. **`celigo_flows.flow_grouping_id` (named in the design spec, §4.1) is
   OMITTED.** observed-shapes.md's live flow probe does not show a
   `flowGroupingId`-shaped field on a FLOW object at all --
   `flowGroupings` was observed on the INTEGRATION object instead. Adding an
   unpopulatable column encodes the spec's stale guess as schema. Left out
   pending a live re-check by whichever task needs it.

6. **Errors are `resolved_at`/`purged_at`, never deleted (global constraint,
   also DON'T #1 in the design spec).** `purge_at` (no `d`) is CELIGO's own
   stated ~30-day deletion deadline, echoed from the API; `purged_at` (with
   `d`) is OUR sync noticing the row is gone from Celigo's listings. Two
   different facts, two different columns, on purpose.

7. **`raw_json` (JSONB) survives on `celigo_integrations`, `celigo_flows`,
   `celigo_flow_steps` alongside the promoted/indexed columns.** The
   sanitizer (Task 1) is the ONLY safety control on what CAN be in that
   JSON (observed-shapes.md: `include`/`exclude` do not reliably strip
   payload fields on the wire, so nothing unsanitized should ever reach
   this migration's INSERTs) -- and its own module docstring records four
   rounds of the allowlist growing to not silently drop real config. Storing
   the full sanitized object, not just today's promoted columns, means a
   fifth allowlist addition doesn't require a new migration to stop being
   silently lost, and gives Task 7's drift-diff something to compare
   against.

8. **FIX ROUND 1: `celigo_connection_id` is `SET NULL` on
   `celigo_flow_errors` / `celigo_error_signatures`, not the shared `CASCADE`
   helper used by the other five tables.** The original version of this
   migration reused `_connection_fk()` (CASCADE, NOT NULL) everywhere,
   including on these two -- which contradicted the audit-table block
   comment two paragraphs above (flow_id/flow_step_id/signature_id are all
   `SET NULL` "because this table must outlive its parents") one column
   over. Deleting the parent `connections` row would have cascaded through
   and destroyed every error row for that connection: exactly the data this
   feature exists to preserve past Celigo's ~30-day purge. Currently latent
   (the product's own delete paths refuse to touch a `provider='celigo'`
   connection row -- see `celigo_write_guard.py`), but that guard's own
   docstring lists raw-SQL writes below the ORM as an explicitly uncovered
   path, so "latent" was never "safe". `_connection_fk_audit()` (nullable,
   `ondelete="SET NULL"`) fixes both tables. The other five
   (integrations/flows/steps/scripts/attachments) keep the CASCADE helper
   on purpose: those rows are a live mirror of Celigo config and are
   meaningless without the connection they came from, unlike the audit
   trail. See `test_celigo_connection_delete_preserves_flow_errors` and
   `test_celigo_connection_delete_preserves_error_signatures`.

Revision ID: 094_celigo_flow_map
Revises: 093_recon_reject_labels
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "094_celigo_flow_map"
down_revision = "093_recon_reject_labels"
branch_labels = None
depends_on = None

# Creation/dependency order (parents before children).
_RLS_TABLES = (
    "celigo_integrations",
    "celigo_flows",
    "celigo_flow_steps",
    "celigo_scripts",
    "celigo_script_attachments",
    "celigo_error_signatures",
    "celigo_flow_errors",
)
# Drop order (children before parents) -- reverse of the above.
_DROP_TABLES = tuple(reversed(_RLS_TABLES))


def _pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant_fk() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)


def _connection_fk() -> sa.Column:
    """CASCADE, NOT NULL -- for the five tables that are a live mirror of
    Celigo config (integrations/flows/steps/scripts/attachments): a row here
    is meaningless without its connection, so deleting the connection should
    remove them too. NOT the two audit tables below -- see
    `_connection_fk_audit()`."""
    return sa.Column(
        "celigo_connection_id", UUID(as_uuid=True), sa.ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )


def _connection_fk_audit() -> sa.Column:
    """SET NULL, nullable -- for `celigo_flow_errors` / `celigo_error_signatures`
    only. These two are the audit trail (design spec G2: "outlives the
    source") -- the same reason flow_id/flow_step_id/signature_id on
    celigo_flow_errors are SET NULL rather than CASCADE. Deleting the parent
    `connections` row must tombstone these rows, never destroy them; a
    CASCADE here would silently wipe the exact evidence this feature exists
    to preserve past Celigo's ~30-day purge."""
    return sa.Column(
        "celigo_connection_id", UUID(as_uuid=True), sa.ForeignKey("connections.id", ondelete="SET NULL"), nullable=True
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # celigo_integrations
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_integrations",
        _pk(),
        _tenant_fk(),
        _connection_fk(),
        sa.Column("celigo_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("celigo_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_integrations_identity"),
    )
    op.create_index("ix_celigo_integrations_tenant_id", "celigo_integrations", ["tenant_id"])
    op.create_index("ix_celigo_integrations_connection_id", "celigo_integrations", ["celigo_connection_id"])

    # ------------------------------------------------------------------
    # celigo_flows
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_flows",
        _pk(),
        _tenant_fk(),
        _connection_fk(),
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("celigo_integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("celigo_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=True),
        sa.Column("schedule", JSONB(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("last_executed_at", sa.DateTime(timezone=True), nullable=True),
        # Flows carry `_sourceId` too, not only scripts (observed-shapes.md
        # finding #1) -- stored, not yet acted on by any dedup logic here.
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("ai_description_summary", sa.Text(), nullable=True),
        sa.Column("ai_description_detailed", sa.Text(), nullable=True),
        sa.Column("ai_description_generated_on", sa.Text(), nullable=True),
        sa.Column("celigo_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_flows_identity"),
    )
    op.create_index("ix_celigo_flows_tenant_id", "celigo_flows", ["tenant_id"])
    op.create_index("ix_celigo_flows_connection_id", "celigo_flows", ["celigo_connection_id"])
    op.create_index("ix_celigo_flows_integration_id", "celigo_flows", ["integration_id"])

    # ------------------------------------------------------------------
    # celigo_flow_steps -- see module docstring deviations 1 and 2
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_flow_steps",
        _pk(),
        _tenant_fk(),
        _connection_fk(),
        sa.Column("flow_id", UUID(as_uuid=True), sa.ForeignKey("celigo_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("celigo_id", sa.Text(), nullable=False),  # the referenced _exportId / _importId
        sa.Column("role", sa.Text(), nullable=False),  # 'generator' | 'processor'
        # Set only for steps sourced from routers[].branches[].pageProcessors
        # (observed-shapes.md: most steps in multi-subsidiary sales-order
        # flows live here, not in the top-level arrays). NULL for top-level
        # pageGenerators/pageProcessors.
        sa.Column("router_id", sa.Text(), nullable=True),
        sa.Column("branch_id", sa.Text(), nullable=True),
        sa.Column(
            "branch_key",
            sa.Text(),
            sa.Computed("COALESCE(branch_id, '$root')", persisted=True),
            nullable=False,
        ),
        # Ordinal position within its containing array (top-level or one
        # branch) -- the flow-map UI (mockup 02) needs original step order,
        # which the unique-key columns above don't preserve.
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        # adaptor_type / connection_celigo_id live on the REFERENCED
        # export/import object, not on the pageProcessor entry itself
        # (sanitizer.py's _PAGE_PROCESSOR has no such fields) -- nullable
        # here, filled in by whichever sync step fetches that object.
        sa.Column("adaptor_type", sa.Text(), nullable=True),
        sa.Column("connection_celigo_id", sa.Text(), nullable=True),
        # filter/responseMapping ARE present directly on the pageProcessor
        # entry (sanitizer.py _PAGE_PROCESSOR), no extra fetch needed.
        sa.Column("filter_json", JSONB(), nullable=True),
        sa.Column("mapping_json", JSONB(), nullable=True),  # responseMapping
        sa.Column("proceed_on_failure", sa.Boolean(), nullable=True),  # processor-only
        sa.Column("skip_retries", sa.Boolean(), nullable=True),  # generator-only
        sa.Column("raw_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.CheckConstraint("role IN ('generator', 'processor')", name="ck_celigo_flow_steps_role"),
        sa.UniqueConstraint(
            "tenant_id",
            "celigo_connection_id",
            "flow_id",
            "celigo_id",
            "branch_key",
            name="uq_celigo_flow_steps_identity",
        ),
    )
    op.create_index("ix_celigo_flow_steps_tenant_id", "celigo_flow_steps", ["tenant_id"])
    op.create_index("ix_celigo_flow_steps_connection_id", "celigo_flow_steps", ["celigo_connection_id"])
    op.create_index("ix_celigo_flow_steps_flow_id", "celigo_flow_steps", ["flow_id"])

    # ------------------------------------------------------------------
    # celigo_scripts -- see module docstring deviation 3
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_scripts",
        _pk(),
        _tenant_fk(),
        _connection_fk(),
        sa.Column("celigo_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),  # absent from list-mode fetches
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),  # NULL on the clone original
        sa.Column(
            "dedup_key",
            sa.Text(),
            sa.Computed("COALESCE(source_id, celigo_id)", persisted=True),
            nullable=False,
        ),
        sa.Column("sandbox", sa.Boolean(), nullable=True),
        sa.Column("celigo_last_modified", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_scripts_identity"),
    )
    op.create_index("ix_celigo_scripts_tenant_id", "celigo_scripts", ["tenant_id"])
    op.create_index("ix_celigo_scripts_connection_id", "celigo_scripts", ["celigo_connection_id"])
    op.create_index("ix_celigo_scripts_dedup_key", "celigo_scripts", ["tenant_id", "celigo_connection_id", "dedup_key"])

    # ------------------------------------------------------------------
    # celigo_script_attachments -- see module docstring deviation 4
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_script_attachments",
        _pk(),
        _tenant_fk(),
        _connection_fk(),
        sa.Column("flow_id", UUID(as_uuid=True), sa.ForeignKey("celigo_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "flow_step_id",
            UUID(as_uuid=True),
            sa.ForeignKey("celigo_flow_steps.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "script_id", UUID(as_uuid=True), sa.ForeignKey("celigo_scripts.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("script_celigo_id", sa.Text(), nullable=False),  # raw _scriptId, always available
        sa.Column("function_name", sa.Text(), nullable=True),
        # Flow-relative, qualified with the owning export/import's celigo id
        # when the ref came off one -- see CeligoScriptAttachment's docstring
        # and repository.qualify_json_path. That qualification is what keeps
        # the unique key below correct for a multi-step flow.
        sa.Column("json_path", sa.Text(), nullable=False),
        sa.Column("site_type", sa.Text(), nullable=True),  # hook | filter | transform | router | unknown
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "flow_id", "json_path", name="uq_celigo_script_attachments_identity"),
    )
    op.create_index("ix_celigo_script_attachments_tenant_id", "celigo_script_attachments", ["tenant_id"])
    op.create_index("ix_celigo_script_attachments_connection_id", "celigo_script_attachments", ["celigo_connection_id"])
    op.create_index("ix_celigo_script_attachments_flow_id", "celigo_script_attachments", ["flow_id"])
    op.create_index("ix_celigo_script_attachments_flow_step_id", "celigo_script_attachments", ["flow_step_id"])
    op.create_index(
        "ix_celigo_script_attachments_script_celigo_id",
        "celigo_script_attachments",
        ["tenant_id", "celigo_connection_id", "script_celigo_id"],
    )

    # ------------------------------------------------------------------
    # celigo_error_signatures
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_error_signatures",
        _pk(),
        _tenant_fk(),
        _connection_fk_audit(),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        # PII-bearing (design spec §4.3) -- never logged, same posture as
        # celigo_flow_errors.message below.
        sa.Column("sample_message", sa.Text(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "celigo_connection_id", "fingerprint", name="uq_celigo_error_signatures_identity"
        ),
    )
    op.create_index("ix_celigo_error_signatures_tenant_id", "celigo_error_signatures", ["tenant_id"])
    op.create_index("ix_celigo_error_signatures_connection_id", "celigo_error_signatures", ["celigo_connection_id"])

    # ------------------------------------------------------------------
    # celigo_flow_errors -- THE audit trail (design spec G2). celigo_connection_id /
    # flow_id / flow_step_id / signature_id are ALL SET NULL on delete, never
    # CASCADE: this table must outlive every one of its parents, the same
    # way it must outlive Celigo's own 30-day purge. resolved_at/purged_at,
    # never a DELETE (global constraint; design spec DON'T #1).
    # ------------------------------------------------------------------
    op.create_table(
        "celigo_flow_errors",
        _pk(),
        _tenant_fk(),
        _connection_fk_audit(),
        sa.Column("flow_id", UUID(as_uuid=True), sa.ForeignKey("celigo_flows.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "flow_step_id",
            UUID(as_uuid=True),
            sa.ForeignKey("celigo_flow_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "signature_id",
            UUID(as_uuid=True),
            sa.ForeignKey("celigo_error_signatures.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("celigo_id", sa.Text(), nullable=False),  # errorId
        sa.Column("trace_key", sa.Text(), nullable=True),
        sa.Column("retry_data_key", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        # Verbatim, deliberately NOT stripped (design spec: "the message IS
        # the diagnosis") -- PII-bearing, never logged.
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_at", sa.DateTime(timezone=True), nullable=True),  # Celigo's own ~30-day deadline
        sa.Column("flow_job_id", sa.Text(), nullable=True),
        # Stored, never branched on (global constraint) -- every current
        # error is retriable=true and none would succeed on retry.
        sa.Column("retriable", sa.Boolean(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),  # WE noticed it vanished from Celigo
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_flow_errors_identity"),
    )
    op.create_index("ix_celigo_flow_errors_tenant_id", "celigo_flow_errors", ["tenant_id"])
    op.create_index("ix_celigo_flow_errors_connection_id", "celigo_flow_errors", ["celigo_connection_id"])
    op.create_index("ix_celigo_flow_errors_flow_id", "celigo_flow_errors", ["flow_id"])
    op.create_index("ix_celigo_flow_errors_flow_step_id", "celigo_flow_errors", ["flow_step_id"])
    op.create_index("ix_celigo_flow_errors_signature_id", "celigo_flow_errors", ["signature_id"])
    # Plan C joins on this (task brief, explicit).
    op.create_index("ix_celigo_flow_errors_tenant_trace_key", "celigo_flow_errors", ["tenant_id", "trace_key"])

    # ------------------------------------------------------------------
    # RLS -- ENABLE + FORCE on all seven, USING and WITH CHECK both pinned
    # to get_current_tenant_id(). No OR-SYSTEM branch: none of these rows are
    # ever SYSTEM-owned, only ever a specific tenant's synced Celigo data.
    # ------------------------------------------------------------------
    for tbl in _RLS_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (tenant_id = get_current_tenant_id())
            WITH CHECK (tenant_id = get_current_tenant_id())
        """)
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")  # load-bearing on Supabase (owner != BYPASSRLS)


def downgrade() -> None:
    for tbl in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
    for tbl in _DROP_TABLES:
        op.drop_table(tbl)
