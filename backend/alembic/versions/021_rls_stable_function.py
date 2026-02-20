"""Standardize RLS policies with stable function wrapper

Creates a STABLE function `get_current_tenant_id()` that caches the tenant UUID
per transaction, avoiding per-row casts. Re-creates all tenant isolation policies
to use the function consistently.

Revision ID: 021_rls_stable_function
Revises: 020_add_auth_type
Create Date: 2026-02-19
"""

from alembic import op

revision = "021_rls_stable_function"
down_revision = "020_add_auth_type"
branch_labels = None
depends_on = None

# --- Migration 001 tables: standard pattern ---
# Policy name: {table}_tenant_isolation
RLS_001_TABLES = [
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

# --- Migration 002 tables ---
# chat_sessions, chat_messages: {table}_tenant_isolation
# doc_chunks: doc_chunks_tenant_isolation (special: allows system tenant)
RLS_002_TABLES = ["chat_sessions", "chat_messages"]

# --- Migration 003 ---
RLS_003_TABLES = ["mcp_connectors"]  # mcp_connectors_tenant_isolation

# --- Migration 007 tables: {table}_tenant_isolation ---
RLS_007_TABLES = [
    "workspaces",
    "workspace_files",
    "workspace_changesets",
    "workspace_patches",
]

# --- Migration 008 tables: {table}_isolation (different naming!) ---
RLS_008_TABLES = [
    "tenant_profiles",
    "policy_profiles",
    "system_prompt_templates",
    "chat_api_keys",
]

# --- Migration 009 tables: {table}_tenant_isolation ---
RLS_009_TABLES = [
    "workspace_runs",
    "workspace_artifacts",
]

# System tenant UUID for doc_chunks (shared docs accessible to all tenants)
SYSTEM_TENANT = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    # 1. Create the stable function
    op.execute("""
        CREATE OR REPLACE FUNCTION get_current_tenant_id() RETURNS uuid
            LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT current_setting('app.current_tenant_id')::uuid $$
    """)

    # 2. Drop and recreate all standard policies (migration 001)
    for table in RLS_001_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = get_current_tenant_id())
        """)

    # 3. Audit events (special: separate SELECT and INSERT policies)
    op.execute("DROP POLICY IF EXISTS audit_events_select ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_insert ON audit_events")
    op.execute("""
        CREATE POLICY audit_events_select ON audit_events
        FOR SELECT USING (tenant_id = get_current_tenant_id())
    """)
    op.execute("""
        CREATE POLICY audit_events_insert ON audit_events
        FOR INSERT WITH CHECK (tenant_id = get_current_tenant_id())
    """)

    # 4. Migration 002 tables
    for table in RLS_002_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = get_current_tenant_id())
        """)

    # doc_chunks: special policy allowing system tenant access
    op.execute("DROP POLICY IF EXISTS doc_chunks_tenant_isolation ON doc_chunks")
    op.execute(f"""
        CREATE POLICY doc_chunks_tenant_isolation ON doc_chunks
        USING (tenant_id = get_current_tenant_id()
               OR tenant_id = '{SYSTEM_TENANT}'::uuid)
    """)

    # 5. Migration 003 tables
    for table in RLS_003_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = get_current_tenant_id())
        """)

    # 6. Migration 007 tables
    for table in RLS_007_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = get_current_tenant_id())
        """)

    # 7. Migration 008 tables (had divergent naming: {table}_isolation)
    for table in RLS_008_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        # Also try standard naming in case it was manually corrected
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = get_current_tenant_id())
        """)

    # 8. Migration 009 tables
    for table in RLS_009_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = get_current_tenant_id())
        """)


def downgrade() -> None:
    # Revert all policies to inline casts

    # Migration 001 tables
    for table in RLS_001_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    # Audit events
    op.execute("DROP POLICY IF EXISTS audit_events_select ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_insert ON audit_events")
    op.execute("""
        CREATE POLICY audit_events_select ON audit_events
        FOR SELECT USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)
    op.execute("""
        CREATE POLICY audit_events_insert ON audit_events
        FOR INSERT WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # Migration 002
    for table in RLS_002_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    op.execute("DROP POLICY IF EXISTS doc_chunks_tenant_isolation ON doc_chunks")
    op.execute(f"""
        CREATE POLICY doc_chunks_tenant_isolation ON doc_chunks
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid
               OR tenant_id = '{SYSTEM_TENANT}'::uuid)
    """)

    # Migration 003
    for table in RLS_003_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    # Migration 007
    for table in RLS_007_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    # Migration 008 — revert to original text-cast pattern
    for table in RLS_008_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_isolation ON {table}
            USING (tenant_id::text = current_setting('app.current_tenant_id', true))
        """)

    # Migration 009
    for table in RLS_009_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    # Drop the function
    op.execute("DROP FUNCTION IF EXISTS get_current_tenant_id()")
