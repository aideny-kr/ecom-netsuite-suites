"""Provision a company-pinned, non-owner runtime after migrations and adoption.

Runs only with separate operator authority on an explicitly identified database.
No automatic password rotation, tenant deletion, or changes to existing policies.
New tables default to inaccessible until this reviewed inventory is reapplied.
"""

import uuid

import asyncpg

from app.services.runtime_security import bigquery_schema_prefix

RUNTIME_ROLE = "suite_runtime"
SYSTEM = uuid.UUID(int=0)
READ_ONLY = {"roles", "permissions", "role_permissions", "alembic_version"}
SYSTEM_READ = {"doc_chunks", "metric_definitions"}
SHARED_TABLES = SYSTEM_READ | {"domain_knowledge_chunks"}


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def provision(
    conn: asyncpg.Connection,
    *,
    database: str,
    cluster: str,
    company: uuid.UUID,
    password: str,
    apply: bool = False,
) -> dict:
    """Preview by default; apply atomically. Credentials are never in the result."""
    if not database or not cluster.isdecimal() or company == SYSTEM or len(password) < 32:
        raise ValueError("Explicit database, cluster, company and a 32+ character runtime password are required")
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute("SELECT pg_advisory_xact_lock(731920260916)")
        actual = await conn.fetchrow(
            "SELECT current_database() db, system_identifier::text cluster FROM pg_control_system()"
        )
        if actual["db"] != database or actual["cluster"] != cluster:
            raise ValueError("Destination cluster/database mismatch")
        if not await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            raise ValueError("Use the isolated database operator, never runtime authority")
        await conn.execute("LOCK TABLE public.tenants IN SHARE ROW EXCLUSIVE MODE")
        rows = await conn.fetch(
            "SELECT id, plan, plan_expires_at, is_active FROM public.tenants WHERE id <> $1", SYSTEM
        )
        if (
            len(rows) != 1
            or rows[0]["id"] != company
            or rows[0]["plan"] != "self_hosted"
            or rows[0]["plan_expires_at"]
            or not rows[0]["is_active"]
        ):
            raise ValueError("Expected exactly the adopted active company; export/import first")
        if await conn.fetchval("""SELECT EXISTS(SELECT FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relkind IN ('S','v','m','f'))"""):
            raise ValueError("Unclassified relations require operator review before provisioning")
        tables = await conn.fetch("""
            SELECT c.relname, c.relrowsecurity,
              EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid=c.oid
                AND a.attname='tenant_id' AND a.attnum>0 AND NOT a.attisdropped) scoped
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname
        """)
        unknown = [
            r["relname"]
            for r in tables
            if not r["scoped"]
            and r["relname"] not in READ_ONLY | {"tenants", "cursor_states", "domain_knowledge_chunks"}
        ]
        if unknown:
            raise ValueError("Unclassified tables require operator review: " + ", ".join(unknown))
        for row in tables:
            if row["scoped"]:
                table = quote(row["relname"])
                await conn.execute(f"LOCK TABLE public.{table} IN SHARE MODE")
                if await conn.fetchval(
                    f"SELECT EXISTS(SELECT FROM public.{table} WHERE tenant_id NOT IN ($1,$2))", company, SYSTEM
                ):
                    raise ValueError("Foreign company rows found; verify the isolated import")
        # Never expose an existing SECURITY DEFINER entrypoint under PUBLIC grants.
        unsafe = await conn.fetchval("""SELECT EXISTS(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND p.prosecdef)""")
        if unsafe:
            raise ValueError("SECURITY DEFINER functions require explicit review before runtime provisioning")
        exists = await conn.fetchval("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=$1)", RUNTIME_ROLE)
        installed = await conn.fetchval("SELECT to_regclass('suite_runtime_meta.binding') IS NOT NULL")
        if exists and not installed:
            raise ValueError("Existing runtime role is not managed by this installer")
        if installed:
            binding = await conn.fetchrow(
                "SELECT company_id, database_name, cluster_id FROM suite_runtime_meta.binding"
            )
            if not binding or tuple(binding) != (company, database, cluster):
                raise ValueError("Existing runtime binding does not match destination")
        if exists:
            r = await conn.fetchrow(
                "SELECT oid, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication "
                "FROM pg_roles WHERE rolname=$1",
                RUNTIME_ROLE,
            )
            if any(r[k] for k in ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication")):
                raise ValueError("Existing runtime role has elevated authority")
            if await conn.fetchval("SELECT EXISTS(SELECT FROM pg_auth_members WHERE member=$1 OR roleid=$1)", r["oid"]):
                raise ValueError("Runtime role memberships are not allowed")
            if await conn.fetchval(
                "SELECT EXISTS(SELECT FROM pg_shdepend WHERE refclassid='pg_authid'::regclass "
                "AND refobjid=$1 AND deptype='o')",
                r["oid"],
            ):
                raise ValueError("Runtime must not own database objects")
        allow_tables = set()
        for row in tables:
            name = row["relname"]
            if name in READ_ONLY:
                continue
            table = "public." + quote(name)
            needs_allow = (
                not row["relrowsecurity"]
                or name in {"tenants", "cursor_states"}
                or await conn.fetchval(
                    "SELECT EXISTS(SELECT FROM pg_policy WHERE polrelid=$1::regclass "
                    "AND polname='suite_runtime_allow')",
                    table,
                )
            )
            if not needs_allow:
                commands = await conn.fetch(
                    "SELECT polcmd::text polcmd FROM pg_policy WHERE polrelid=$1::regclass AND polpermissive "
                    "AND (0=ANY(polroles) OR (SELECT oid FROM pg_roles WHERE rolname=$2)=ANY(polroles))",
                    table,
                    RUNTIME_ROLE,
                )
                covered = {r["polcmd"] for r in commands}
                required = {"r", "a"} if name == "audit_events" else {"r", "a", "w", "d"}
                if "*" not in covered and not required.issubset(covered):
                    raise ValueError(
                        f"Existing RLS lacks runtime command coverage on {name}; review policies before provisioning"
                    )
            if needs_allow:
                allow_tables.add(name)
        result = {
            "tables": len(tables),
            "company_tables": sum(r["scoped"] for r in tables),
            "applied": apply,
            "role": RUNTIME_ROLE,
            "password_set": apply and not exists,
        }
        if not apply:
            return result
        if not exists:
            # Suppress standard PostgreSQL statement/error logging for the
            # credential-bearing DDL; transaction-local settings revert on exit.
            # The CLI also suppresses exception bodies and accepts a private file.
            await conn.execute("SET LOCAL log_statement = 'none'")
            await conn.execute("SET LOCAL log_min_error_statement = 'panic'")
            escaped = password.replace("'", "''")
            await conn.execute(
                f"CREATE ROLE {RUNTIME_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
                f"NOREPLICATION NOINHERIT PASSWORD '{escaped}'"
            )
        await conn.execute("CREATE SCHEMA IF NOT EXISTS suite_runtime_meta")
        await conn.execute("REVOKE ALL ON SCHEMA suite_runtime_meta FROM PUBLIC")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS suite_runtime_meta.binding "
            "(singleton bool PRIMARY KEY DEFAULT true CHECK(singleton), "
            "company_id uuid NOT NULL, database_name text NOT NULL, cluster_id text NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO suite_runtime_meta.binding(company_id,database_name,cluster_id) "
            "VALUES($1,$2,$3) ON CONFLICT(singleton) DO NOTHING",
            company,
            database,
            cluster,
        )
        await conn.execute(f"GRANT USAGE ON SCHEMA public, suite_runtime_meta TO {RUNTIME_ROLE}")
        await conn.execute(f"GRANT SELECT ON suite_runtime_meta.binding TO {RUNTIME_ROLE}")
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        await conn.execute(f"REVOKE ALL ON DATABASE {quote(database)} FROM PUBLIC")
        await conn.execute(f"GRANT CONNECT ON DATABASE {quote(database)} TO {RUNTIME_ROLE}")
        await conn.execute(
            f"ALTER ROLE {RUNTIME_ROLE} IN DATABASE {quote(database)} SET app.current_tenant_id = '{company}'"
        )
        await conn.execute(f"ALTER ROLE {RUNTIME_ROLE} IN DATABASE {quote(database)} SET search_path = public")
        await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, {RUNTIME_ROLE}")
        await conn.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, {RUNTIME_ROLE}")
        # Literal company binding is immutable to runtime, unlike the freely
        # settable tenant GUC. Restrictive policies AND with existing policies.
        for row in tables:
            name = row["relname"]
            table = "public." + quote(name)
            if name in READ_ONLY:
                await conn.execute(f"GRANT SELECT ON {table} TO {RUNTIME_ROLE}")
                continue
            await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            if name == "domain_knowledge_chunks":
                # Global curated knowledge stays read-only. The existing BQ
                # selection flow can replace only this company's schema rows.
                # Legacy unscoped BQ rows are hidden until verified re-discovery.
                own_schema = (
                    "source_type = 'bigquery_schema' AND partition_id = 'bi/schema-docs' "
                    f"AND starts_with(source_uri, '{bigquery_schema_prefix(company)}')"
                )
                predicate = f"(source_type <> 'bigquery_schema' OR ({own_schema}))"
                write = f"({own_schema}) AND public.get_current_tenant_id() = '{company}'::uuid"
                grants = "SELECT, INSERT, UPDATE, DELETE"
            elif name == "tenants":
                predicate = f"id = '{company}'::uuid"
                write = predicate
                grants = "SELECT, UPDATE"
            elif name == "cursor_states":
                predicate = "EXISTS (SELECT 1 FROM public.connections c WHERE c.id = connection_id)"
                write = predicate
                grants = "SELECT, INSERT, UPDATE, DELETE"
            else:
                predicate = f"tenant_id = '{company}'::uuid"
                write = predicate
                if name in SYSTEM_READ:
                    predicate = f"({predicate} OR tenant_id = '{SYSTEM}'::uuid)"
                grants = "SELECT, INSERT" if name == "audit_events" else "SELECT, INSERT, UPDATE, DELETE"
            needs_allow = name in allow_tables
            for policy in (
                "suite_runtime_allow",
                "suite_runtime_bound",
                "suite_runtime_update",
                "suite_runtime_delete",
            ):
                await conn.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
            # Existing tables retain their policies. New/missing RLS tables get
            # the standard context predicate, with explicit shared read-only rows.
            if needs_allow:
                allow = predicate if not row["scoped"] else "tenant_id = public.get_current_tenant_id()"
                await conn.execute(
                    f"CREATE POLICY suite_runtime_allow ON {table} TO {RUNTIME_ROLE} "
                    f"USING ({allow}) WITH CHECK ({write})"
                )
            await conn.execute(
                f"CREATE POLICY suite_runtime_bound ON {table} AS RESTRICTIVE TO {RUNTIME_ROLE} "
                f"USING ({predicate}) WITH CHECK ({write})"
            )
            if name in SHARED_TABLES:
                # A read-visible SYSTEM row must not be deletable or movable
                # into company ownership by UPDATE tenant_id.
                await conn.execute(
                    f"CREATE POLICY suite_runtime_update ON {table} AS RESTRICTIVE "
                    f"FOR UPDATE TO {RUNTIME_ROLE} USING ({write}) WITH CHECK ({write})"
                )
                await conn.execute(
                    f"CREATE POLICY suite_runtime_delete ON {table} AS RESTRICTIVE "
                    f"FOR DELETE TO {RUNTIME_ROLE} USING ({write})"
                )
            await conn.execute(f"GRANT {grants} ON {table} TO {RUNTIME_ROLE}")
        return result
