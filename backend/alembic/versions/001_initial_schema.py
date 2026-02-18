"""Initial schema with RLS policies and seed data

Revision ID: 001_initial
Revises:
Create Date: 2026-02-16
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None

# Seed data
ROLES = ["admin", "finance", "ops", "readonly"]

PERMISSIONS = [
    "tenant.manage",
    "users.manage",
    "connections.manage",
    "connections.view",
    "tables.view",
    "audit.view",
    "exports.csv",
    "exports.excel",
    "recon.run",
    "tools.suiteql",
    "schedules.manage",
    "approvals.manage",
]

# Role -> permissions mapping
ROLE_PERMISSIONS = {
    "admin": PERMISSIONS,  # All permissions
    "finance": [
        "connections.view",
        "tables.view",
        "audit.view",
        "exports.csv",
        "exports.excel",
        "recon.run",
        "tools.suiteql",
    ],
    "ops": [
        "connections.manage",
        "connections.view",
        "tables.view",
        "audit.view",
        "exports.csv",
        "schedules.manage",
    ],
    "readonly": ["connections.view", "tables.view", "audit.view"],
}

# Tables that need RLS with standard tenant_id policy
RLS_TABLES = [
    "tenant_configs",
    "users",
    "user_roles",
    "connections",
    "jobs",
    "orders",
    "payments",
    "refunds",
    "payouts",
    "payout_lines",
    "disputes",
    "netsuite_postings",
    "evidence_packs",
    "schedules",
]


def upgrade() -> None:
    # ---- Core tables ----
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), unique=True, nullable=False),
        sa.Column("plan", sa.String(50), server_default="trial", nullable=False),
        sa.Column("plan_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "tenant_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), unique=True, nullable=False),
        sa.Column("subsidiaries", JSON, nullable=True),
        sa.Column("account_mappings", JSON, nullable=True),
        sa.Column("posting_mode", sa.String(50), server_default="lumpsum", nullable=False),
        sa.Column("posting_batch_size", sa.Integer, server_default="100", nullable=False),
        sa.Column("posting_attach_evidence", sa.Boolean, server_default="false", nullable=False),
        sa.Column("netsuite_account_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("actor_type", sa.String(50), server_default="user", nullable=False),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "roles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(50), unique=True, nullable=False),
    )

    op.create_table(
        "permissions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("codename", sa.String(100), unique=True, nullable=False),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", UUID(as_uuid=True), sa.ForeignKey("roles.id"), primary_key=True),
        sa.Column("permission_id", UUID(as_uuid=True), sa.ForeignKey("permissions.id"), primary_key=True),
    )

    op.create_table(
        "user_roles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role_id", UUID(as_uuid=True), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_user_roles_tenant_id", "user_roles", ["tenant_id"])
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])

    op.create_table(
        "connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("status", sa.String(50), server_default="active", nullable=False),
        sa.Column("encrypted_credentials", sa.Text, nullable=False),
        sa.Column("encryption_key_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("metadata_json", JSON, nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_connections_tenant_id", "connections", ["tenant_id"])

    # ---- Audit + Jobs ----
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(50), server_default="user", nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        sa.Column("job_id", UUID(as_uuid=True), nullable=True),
        sa.Column("payload", JSON, nullable=True),
        sa.Column("status", sa.String(50), server_default="success", nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_category", "audit_events", ["category"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])

    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("connections.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parameters", JSON, nullable=True),
        sa.Column("result_summary", JSON, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_correlation_id", "jobs", ["correlation_id"])

    # ---- Canonical tables ----
    canonical_common = [
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("dedupe_key", sa.String(512), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("subsidiary_id", sa.String(255), nullable=True),
        sa.Column("raw_data", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]

    op.create_table(
        "orders",
        *canonical_common,
        sa.Column("order_number", sa.String(255), nullable=False),
        sa.Column("customer_email", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("subtotal", sa.Numeric(15, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(15, 2), server_default="0", nullable=False),
        sa.Column("discount_amount", sa.Numeric(15, 2), server_default="0", nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_orders_dedupe"),
    )
    op.create_index("ix_orders_tenant_id", "orders", ["tenant_id"])

    op.create_table(
        "payments",
        *[c.copy() for c in canonical_common],
        sa.Column("order_id", UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("payment_method", sa.String(100), nullable=True),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_payments_dedupe"),
    )
    op.create_index("ix_payments_tenant_id", "payments", ["tenant_id"])

    op.create_table(
        "refunds",
        *[c.copy() for c in canonical_common],
        sa.Column("order_id", UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_refunds_dedupe"),
    )
    op.create_index("ix_refunds_tenant_id", "refunds", ["tenant_id"])

    op.create_table(
        "payouts",
        *[c.copy() for c in canonical_common],
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("fee_amount", sa.Numeric(15, 2), server_default="0", nullable=False),
        sa.Column("net_amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("arrival_date", sa.Date, nullable=True),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_payouts_dedupe"),
    )
    op.create_index("ix_payouts_tenant_id", "payouts", ["tenant_id"])

    op.create_table(
        "payout_lines",
        *[c.copy() for c in canonical_common],
        sa.Column("payout_id", UUID(as_uuid=True), sa.ForeignKey("payouts.id"), nullable=True),
        sa.Column("line_type", sa.String(100), nullable=False),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("fee", sa.Numeric(15, 2), server_default="0", nullable=False),
        sa.Column("net", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("related_order_id", sa.String(255), nullable=True),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_payout_lines_dedupe"),
    )
    op.create_index("ix_payout_lines_tenant_id", "payout_lines", ["tenant_id"])

    op.create_table(
        "disputes",
        *[c.copy() for c in canonical_common],
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("related_order_id", sa.String(255), nullable=True),
        sa.Column("related_payment_id", sa.String(255), nullable=True),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_disputes_dedupe"),
    )
    op.create_index("ix_disputes_tenant_id", "disputes", ["tenant_id"])

    op.create_table(
        "netsuite_postings",
        *[c.copy() for c in canonical_common],
        sa.Column("netsuite_internal_id", sa.String(255), nullable=True),
        sa.Column("record_type", sa.String(100), nullable=False),
        sa.Column("transaction_date", sa.Date, nullable=True),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=True),
        sa.Column("account_name", sa.String(255), nullable=True),
        sa.Column("memo", sa.Text, nullable=True),
        sa.Column("related_payout_id", sa.String(255), nullable=True),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_netsuite_postings_dedupe"),
    )
    op.create_index("ix_netsuite_postings_tenant_id", "netsuite_postings", ["tenant_id"])

    # ---- Pipeline tables ----
    op.create_table(
        "cursor_states",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("object_type", sa.String(100), nullable=False),
        sa.Column("cursor_value", sa.String(512), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("connection_id", "object_type", name="uq_cursor_states_conn_obj"),
    )
    op.create_index("ix_cursor_states_connection_id", "cursor_states", ["connection_id"])

    op.create_table(
        "evidence_packs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("pack_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("storage_uri", sa.Text, nullable=True),
        sa.Column("file_format", sa.String(50), nullable=True),
        sa.Column("metadata_json", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_evidence_packs_tenant_id", "evidence_packs", ["tenant_id"])

    op.create_table(
        "schedules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("schedule_type", sa.String(100), nullable=False),
        sa.Column("cron_expression", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("parameters", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_schedules_tenant_id", "schedules", ["tenant_id"])

    # ---- RLS Policies ----
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    # Special RLS for audit_events: INSERT + SELECT only, no UPDATE/DELETE
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_events_select ON audit_events
        FOR SELECT USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)
    op.execute("""
        CREATE POLICY audit_events_insert ON audit_events
        FOR INSERT WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # ---- Seed Roles + Permissions ----
    roles_table = sa.table("roles", sa.column("id", UUID), sa.column("name", sa.String))
    perms_table = sa.table("permissions", sa.column("id", UUID), sa.column("codename", sa.String))
    rp_table = sa.table(
        "role_permissions",
        sa.column("role_id", UUID),
        sa.column("permission_id", UUID),
    )

    role_ids = {}
    for role_name in ROLES:
        rid = uuid.uuid4()
        role_ids[role_name] = rid
        op.execute(roles_table.insert().values(id=rid, name=role_name))

    perm_ids = {}
    for perm in PERMISSIONS:
        pid = uuid.uuid4()
        perm_ids[perm] = pid
        op.execute(perms_table.insert().values(id=pid, codename=perm))

    for role_name, perms in ROLE_PERMISSIONS.items():
        for perm in perms:
            op.execute(
                rp_table.insert().values(
                    role_id=role_ids[role_name],
                    permission_id=perm_ids[perm],
                )
            )


def downgrade() -> None:
    # Drop RLS policies
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS audit_events_select ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_insert ON audit_events")
    op.execute("ALTER TABLE audit_events DISABLE ROW LEVEL SECURITY")

    # Drop tables in reverse dependency order
    tables = [
        "schedules",
        "evidence_packs",
        "cursor_states",
        "netsuite_postings",
        "disputes",
        "payout_lines",
        "payouts",
        "refunds",
        "payments",
        "orders",
        "jobs",
        "audit_events",
        "connections",
        "user_roles",
        "role_permissions",
        "permissions",
        "roles",
        "users",
        "tenant_configs",
        "tenants",
    ]
    for table in tables:
        op.drop_table(table)
