"""Operator-only selective logical snapshots. No application or worker is started.

Policies and archives contain private company data and must stay outside Git.
Export owns one PostgreSQL repeatable-read, read-only transaction.
"""

import base64
import gzip
import hashlib
import json
import uuid

MAX_FRAME_SIZE = 1024 * 1024
PORTABLE_TYPES = {
    "bool",
    "int2",
    "int4",
    "int8",
    "float4",
    "float8",
    "numeric",
    "bytea",
    "text",
    "varchar",
    "bpchar",
    "uuid",
    "json",
    "jsonb",
    "date",
    "time",
    "timetz",
    "timestamp",
    "timestamptz",
    "interval",
    "inet",
    "cidr",
    "macaddr",
    "macaddr8",
    "bit",
    "varbit",
}
SYSTEM = uuid.UUID(int=0)
SUPPORTED_EXTENSIONS = {"plpgsql", "vector", "pgcrypto", "uuid-ossp", "pg_trgm", "btree_gin"}
GLOBAL_TABLES = {"roles", "permissions", "role_permissions", "alembic_version"}


def quoted(value):
    return '"' + value.replace('"', '""') + '"'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


async def database_identity(conn):
    return dict(
        await conn.fetchrow(
            "SELECT current_database() AS database, system_identifier::text AS cluster FROM pg_control_system()"
        )
    )


async def table_names(conn):
    return [
        r["relname"]
        for r in await conn.fetch("""
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','f') ORDER BY c.relname
    """)
    ]


async def schema_inventory(conn):
    unsupported_types = await conn.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid
        JOIN pg_namespace tn ON tn.oid=t.typnamespace
        LEFT JOIN pg_type elem ON elem.oid=t.typelem LEFT JOIN pg_namespace en ON en.oid=elem.typnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','f') AND a.attnum>0 AND NOT a.attisdropped
        AND NOT ((tn.nspname='pg_catalog' AND t.typname=ANY($1::text[]))
          OR (tn.nspname='pg_catalog' AND t.typcategory='A' AND en.nspname='pg_catalog'
              AND elem.typname=ANY($1::text[]))
          OR (t.typname='vector' AND EXISTS (
            SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
            WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e' AND e.extname='vector'))))
    """,
        sorted(PORTABLE_TYPES),
    )
    if unsupported_types:
        raise ValueError("Column types need explicit cross-cluster binary compatibility review")
    rows = await conn.fetch("""
        SELECT c.relname AS name, c.relkind, a.attname AS column_name,
               format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null,
               a.attgenerated AS generated, a.attidentity AS identity,
               pg_get_expr(d.adbin,d.adrelid) AS default_expression,
               c.relrowsecurity, c.relforcerowsecurity,
               cn.nspname AS collation_schema, co.collname AS collation_name,
               co.collprovider::text AS collation_provider, co.collisdeterministic AS deterministic,
               co.collcollate,co.collctype,co.collversion,
               COALESCE(to_jsonb(co)->>'colllocale',to_jsonb(co)->>'colliculocale') AS collation_locale,
               CASE WHEN co.collprovider<>'d' THEN pg_collation_actual_version(co.oid) END AS actual_version
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        LEFT JOIN pg_collation co ON co.oid=a.attcollation LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','f')
          AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attname
    """)
    tables = {}
    for row in rows:
        if row["relkind"] != b"r":
            # asyncpg returns PostgreSQL's internal char type as bytes.
            raise ValueError("Only ordinary public tables are supported")
        if row["identity"] not in {b"", b"\x00"} or row["generated"] not in {b"", b"\x00", b"s"}:
            raise ValueError("Identity/virtual generated columns need an explicit restore design")
        table = tables.setdefault(row["name"], {"columns": [], "constraints": [], "order": []})
        table["row_security"] = [row["relrowsecurity"], row["relforcerowsecurity"]]
        table["columns"].append(
            {
                "name": row["column_name"],
                "type": row["type"],
                "not_null": row["not_null"],
                "default": row["default_expression"],
                "generated": row["generated"] == b"s",
                "collation": {
                    "schema": row["collation_schema"],
                    "name": row["collation_name"],
                    "provider": row["collation_provider"],
                    "deterministic": row["deterministic"],
                    "collate": row["collcollate"],
                    "ctype": row["collctype"],
                    "locale": row["collation_locale"],
                    "version": row["collversion"],
                    "actual_version": row["actual_version"],
                }
                if row["collation_name"]
                else None,
            }
        )
    constraints = await conn.fetch("""
        SELECT c.relname AS table_name, k.conname AS name, k.contype::text AS kind,
               pg_get_constraintdef(k.oid) AS definition,
               ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY x(num,ord)
                     JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.num ORDER BY x.ord) AS columns
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        WHERE k.connamespace='public'::regnamespace ORDER BY c.relname,k.conname
    """)
    for row in constraints:
        if row["table_name"] in tables:
            table = tables[row["table_name"]]
            table["constraints"].append({k: row[k] for k in ("name", "kind", "definition")})
            if row["kind"] == "p":
                table["order"] = list(row["columns"])
    if set(tables) != set(await table_names(conn)) or not tables or any(not t["order"] for t in tables.values()):
        raise ValueError("Every exported table must have a primary key for deterministic verification")
    extensions = [
        dict(r)
        for r in await conn.fetch(
            "SELECT extname,extversion FROM pg_extension WHERE extname=ANY($1::text[]) ORDER BY extname",
            sorted(SUPPORTED_EXTENSIONS),
        )
    ]
    objects = {}
    queries = {
        "rules": (
            "SELECT tablename,rulename,definition FROM pg_rules WHERE schemaname='public' ORDER BY tablename,rulename"
        ),
        "indexes": """
            SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY
            tablename,indexname
        """,
        "triggers": """
            SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid) AS definition,t.tgenabled::text AS enabled FROM
            pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE
            n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname
        """,
        "policies": """
            SELECT tablename,policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE
            schemaname='public' ORDER BY tablename,policyname
        """,
        "functions": """
            SELECT p.oid::regprocedure::text AS signature,pg_get_functiondef(p.oid) AS definition FROM pg_proc
            p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind IN ('f','p')
            AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid
            AND d.deptype='e') ORDER BY 1
        """,
    }
    for name, sql in queries.items():
        objects[name] = [dict(row) for row in await conn.fetch(sql)]
    if objects["rules"] or any(t["enabled"] in {"A", "R"} for t in objects["triggers"]):
        raise ValueError("Public rules or ALWAYS/REPLICA triggers need an explicit restore design")
    database_locale = dict(
        await conn.fetchrow("""
        SELECT pg_encoding_to_char(encoding) AS encoding,datcollate,datctype,datlocprovider::text,datcollversion,
               pg_database_collation_actual_version(oid) AS actual_version,
               COALESCE(to_jsonb(d)->>'datlocale',to_jsonb(d)->>'daticulocale') AS locale
        FROM pg_database d WHERE datname=current_database()
    """)
    )
    unsupported = await conn.fetchval("""
        SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('v','m','S') AND NOT EXISTS (
            SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'))
    """)
    if unsupported:
        raise ValueError("Views/materialized views/sequences need an explicit snapshot design")
    return {
        "tables": tables,
        "extensions": extensions,
        "objects": objects,
        "postgres_major": int(await conn.fetchval("SHOW server_version_num")) // 10000,
        "database_locale": database_locale,
    }


def selection(table, rule, policy, columns):
    company = str(uuid.UUID(policy["tenant_id"]))
    if uuid.UUID(company) == SYSTEM:
        raise ValueError("A non-system company is required")
    scope = rule.get("scope")
    if set(rule) - {"scope", "ids", "shared_ids"}:
        raise ValueError("Unknown selection policy fields")
    ids = [str(uuid.UUID(value)) for value in rule.get("ids", [])]
    shared = [str(uuid.UUID(value)) for value in rule.get("shared_ids", [])]
    if len(ids) != len(set(ids)) or len(shared) != len(set(shared)):
        raise ValueError("Duplicate reviewed row IDs")
    if ids and scope != "ids" or shared and scope != "tenant":
        raise ValueError("Reviewed row IDs do not match the selection scope")
    if scope == "all" and table in GLOBAL_TABLES and "tenant_id" not in columns:
        return "TRUE"
    if scope == "company" and table == "tenants":
        return f"id IN ('{company}'::uuid,'{SYSTEM}'::uuid)"
    if scope == "connections" and table == "cursor_states":
        return f"connection_id IN (SELECT id FROM public.connections WHERE tenant_id='{company}'::uuid)"
    if scope == "ids" and table == "domain_knowledge_chunks":
        return "id IN (" + ",".join(f"'{v}'::uuid" for v in ids) + ")" if ids else "FALSE"
    if scope == "tenant" and "tenant_id" in columns:
        result = f"tenant_id='{company}'::uuid"
        if shared:
            if "id" not in columns:
                raise ValueError("Shared row review requires a row ID")
            result += " OR (id IN (" + ",".join(f"'{v}'::uuid" for v in shared)
            result += f") AND (tenant_id IS NULL OR tenant_id='{SYSTEM}'::uuid))"
        return "(" + result + ")"
    raise ValueError("Unreviewed or incompatible table selection: " + table)


async def validate_policy(conn, policy, schema):
    if set(policy) != {"version", "tenant_id", "tenant_slug", "schema_revision", "tables"} or policy["version"] != 1:
        raise ValueError("Unsupported policy format")
    if set(policy["tables"]) != set(schema["tables"]):
        raise ValueError("Every source table must be explicitly classified; schema changed")
    revision = await conn.fetch("SELECT version_num FROM public.alembic_version ORDER BY version_num")
    if [r["version_num"] for r in revision] != policy["schema_revision"]:
        raise ValueError("Source schema revision does not match the reviewed policy")
    company = uuid.UUID(policy["tenant_id"])
    slug = await conn.fetchval("SELECT slug FROM public.tenants WHERE id=$1", company)
    if not policy["tenant_slug"] or slug != policy["tenant_slug"]:
        raise ValueError("Source company UUID/slug does not match")
    selectors = {}
    for name, table in schema["tables"].items():
        columns = [c["name"] for c in table["columns"]]
        rule = policy["tables"][name]
        selectors[name] = selection(name, rule, policy, columns)
        for key in ("ids", "shared_ids"):
            if rule.get(key):
                ids = [uuid.UUID(v) for v in rule[key]]
                found = await conn.fetchval(
                    f"SELECT count(*) FROM public.{quoted(name)} WHERE ({selectors[name]}) AND id=ANY($1::uuid[])", ids
                )
                if found != len(ids):
                    raise ValueError("Reviewed shared row missing or outside scope: " + name)
    return selectors


async def copy_table(conn, name, table, where, sink, *, full=False):
    columns = ",".join(quoted(c["name"]) for c in table["columns"] if full or not c["generated"])
    collatable = {c["name"] for c in table["columns"] if c["collation"]}
    order = ",".join(quoted(c) + (' COLLATE pg_catalog."C"' if c in collatable else "") for c in table["order"])
    digest = hashlib.sha256()

    async def write(data):
        digest.update(data)
        if sink:
            await sink(data)

    status = await conn.copy_from_query(
        f"SELECT {columns} FROM public.{quoted(name)} WHERE {where} ORDER BY {order}",
        output=write,
        format="binary",
    )
    return {"rows": int(status.split()[-1]), "sha256": digest.hexdigest()}


async def export_snapshot(conn, policy, output):
    if conn.is_in_transaction():
        raise ValueError("Snapshot export must own its transaction")
    # Discovery must precede the snapshot transaction. LOCK must precede its first
    # SELECT: TRUNCATE/table rewrites otherwise make old snapshots see empty tables.
    # https://www.postgresql.org/docs/17/sql-lock.html
    names = await table_names(conn)
    if not names:
        raise ValueError("No public tables to export")
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SET LOCAL row_security=off")
        await conn.execute("SET LOCAL lock_timeout='5s'")
        await conn.execute("SET LOCAL statement_timeout='15min'")
        await conn.execute("SET LOCAL idle_in_transaction_session_timeout='15min'")
        await conn.execute("LOCK TABLE " + ",".join("public." + quoted(n) for n in names) + " IN ACCESS SHARE MODE")
        schema = await schema_inventory(conn)
        if list(schema["tables"]) != names:
            raise ValueError("Source table inventory changed during lock acquisition")
        selectors = await validate_policy(conn, policy, schema)
        header = {
            "format": 1,
            "source": await database_identity(conn),
            "schema": schema,
            "policy": policy,
            "snapshot": await conn.fetchval("SELECT pg_current_snapshot()::text"),
        }
        result = {"source": header["source"], "snapshot": header["snapshot"], "tables": {}}
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as archive:

            def emit(value):
                frame = canonical(value) + b"\n"
                if len(frame) > MAX_FRAME_SIZE:
                    raise ValueError("Oversized snapshot frame")
                archive.write(frame)

            emit({"header": header})
            for name, table in schema["tables"].items():
                emit({"table": name})

                async def sink(data):
                    for offset in range(0, len(data), 65536):
                        emit({"data": base64.b64encode(data[offset : offset + 65536]).decode("ascii")})

                info = await copy_table(conn, name, table, selectors[name], sink)
                if any(c["generated"] for c in table["columns"]):
                    info["full_sha256"] = (await copy_table(conn, name, table, selectors[name], None, full=True))[
                        "sha256"
                    ]
                result["tables"][name] = info
                emit({"end_table": info})
            emit({"complete": result})
        return result


async def verify_foreign_keys(conn):
    keys = await conn.fetch("""
        SELECT k.conname AS name, c.relname AS child, p.relname AS parent,
               pn.nspname AS parent_schema, k.confmatchtype::text AS match_type,
               ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY x(num,ord)
                     JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.num ORDER BY x.ord) AS child_columns,
               ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY x(num,ord)
                     JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.num ORDER BY x.ord) AS parent_columns
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_class p ON p.oid=k.confrelid JOIN pg_namespace pn ON pn.oid=p.relnamespace
        WHERE k.connamespace='public'::regnamespace AND k.contype='f' ORDER BY c.relname,k.conname
    """)
    for key in keys:
        if key["parent_schema"] != "public" or key["match_type"] not in {"s", "f"}:
            raise ValueError("Foreign key needs an explicit restore design: " + key["name"])
        children = ["c." + quoted(n) for n in key["child_columns"]]
        parents = ["p." + quoted(n) for n in key["parent_columns"]]
        all_present = " AND ".join(n + " IS NOT NULL" for n in children)
        match = " AND ".join(a + "=" + b for a, b in zip(children, parents, strict=True))
        missing = f"({all_present}) AND NOT EXISTS (SELECT 1 FROM public.{quoted(key['parent'])} p WHERE {match})"
        if key["match_type"] == "f":
            any_present = " OR ".join(n + " IS NOT NULL" for n in children)
            any_null = " OR ".join(n + " IS NULL" for n in children)
            missing = f"({missing}) OR (({any_present}) AND ({any_null}))"
        if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM public.{quoted(key['child'])} c WHERE {missing})"):
            raise ValueError("Snapshot has a missing foreign-key reference: " + key["name"])
    return len(keys)


def read_frame(archive):
    line = archive.readline(MAX_FRAME_SIZE + 1)
    if not line or len(line) > MAX_FRAME_SIZE or not line.endswith(b"\n"):
        raise ValueError("Incomplete or oversized snapshot frame")
    value = json.loads(line)
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("Invalid snapshot frame")
    return value


async def restore_snapshot(conn, policy, source, *, database, cluster):
    """Atomic import into an empty, independently provisioned offline destination.

    Operator identity comes from provisioning records. No deletes/upserts/resume.
    Disable triggers only within the import transaction to retain exact stored data;
    all declared FKs, scope and deterministic COPY digests are checked before commit.
    """
    if conn.is_in_transaction():
        raise ValueError("Snapshot restore must own its transaction")
    if not database or not cluster.isdecimal():
        raise ValueError("An explicit provisioned destination identity is required")
    with gzip.GzipFile(fileobj=source, mode="rb") as archive:
        first = read_frame(archive)
        header = first.get("header", {})
        if header.get("format") != 1 or header.get("policy") != policy:
            raise ValueError("Snapshot does not match the reviewed policy")
        origin = header.get("source", {})
        if not str(origin.get("cluster", "")).isdecimal() or not origin.get("database"):
            raise ValueError("Snapshot lacks source identity")
        if origin["cluster"] == cluster:
            raise ValueError("Source and destination must be independently initialized clusters")
        async with conn.transaction():
            if await database_identity(conn) != {"database": database, "cluster": cluster}:
                raise ValueError("Destination database/cluster does not match provisioning record")
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute("SET LOCAL row_security=off")
            names = await table_names(conn)
            if set(names) != set(policy["tables"]):
                raise ValueError("Destination schema differs from the snapshot")
            await conn.execute(
                "LOCK TABLE " + ",".join("public." + quoted(n) for n in names) + " IN ACCESS EXCLUSIVE MODE"
            )
            schema = await schema_inventory(conn)
            if schema != header.get("schema") or list(schema["tables"]) != names:
                raise ValueError("Destination schema differs from the snapshot")
            for name in schema["tables"]:
                if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM public.{quoted(name)})"):
                    raise ValueError("Destination must be empty; existing or partial imports are never overwritten")
            await conn.execute("SET LOCAL session_replication_role=replica")
            imported = {}
            for name, table in schema["tables"].items():
                if read_frame(archive) != {"table": name}:
                    raise ValueError("Snapshot tables are missing, duplicated or reordered")
                digest = hashlib.sha256()
                expected = None

                async def chunks():
                    nonlocal expected
                    while True:
                        frame = read_frame(archive)
                        if "end_table" in frame:
                            expected = frame["end_table"]
                            return
                        if "data" not in frame:
                            raise ValueError("Invalid snapshot table data")
                        data = base64.b64decode(frame["data"], validate=True)
                        digest.update(data)
                        yield data

                status = await conn.copy_to_table(
                    name,
                    schema_name="public",
                    columns=[c["name"] for c in table["columns"] if not c["generated"]],
                    source=chunks(),
                    format="binary",
                )
                actual = {"rows": int(status.split()[-1]), "sha256": digest.hexdigest()}
                if any(c["generated"] for c in table["columns"]):
                    actual["full_sha256"] = (await copy_table(conn, name, table, "TRUE", None, full=True))["sha256"]
                if expected != actual:
                    raise ValueError("Snapshot count/content checksum failed: " + name)
                imported[name] = actual
            complete = read_frame(archive)
            if complete != {"complete": {"source": origin, "snapshot": header["snapshot"], "tables": imported}}:
                raise ValueError("Snapshot completion record does not match")
            if archive.read(1):
                raise ValueError("Snapshot has unexpected trailing data")
            selectors = await validate_policy(conn, policy, schema)
            for name, table in schema["tables"].items():
                # IS NOT TRUE also rejects NULL predicates (foreign/unreviewed nullable scope).
                if await conn.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM public.{quoted(name)} WHERE ({selectors[name]}) IS NOT TRUE)"
                ):
                    raise ValueError("Restored rows are outside reviewed company scope: " + name)
                stored = await copy_table(conn, name, table, "TRUE", None)
                if any(c["generated"] for c in table["columns"]):
                    stored["full_sha256"] = (await copy_table(conn, name, table, "TRUE", None, full=True))["sha256"]
                if stored != imported[name]:
                    raise ValueError("Restored content differs from the source: " + name)
            keys = await verify_foreign_keys(conn)
            await conn.execute("SET LOCAL session_replication_role=origin")
        return {
            "verified": True,
            "destination": {"database": database, "cluster": cluster},
            "source": origin,
            "snapshot": header["snapshot"],
            "foreign_keys_verified": keys,
            "tables": imported,
        }


def validate_destination_url(dsn, database):
    from urllib.parse import urlsplit

    url = urlsplit(dsn)
    try:
        port = url.port  # Reject malformed, multi-host and out-of-range port strings.
    except ValueError:
        raise ValueError("Invalid destination host/port") from None
    if (
        url.scheme not in {"postgresql", "postgres"}
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.path != "/" + database
        or not database.endswith("_rehearsal")
        or url.query
        or "," in url.netloc
        or port == 0
    ):
        raise ValueError("Restore requires an explicit loopback database ending in _rehearsal, without URL overrides")


def main():
    import argparse
    import asyncio
    import os
    import sys
    from pathlib import Path

    import asyncpg

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "restore"])
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-cluster")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    os.umask(0o077)

    async def run():
        dsn = os.environ.get("SNAPSHOT_DATABASE_URL")
        if not dsn:
            raise ValueError("Set SNAPSHOT_DATABASE_URL explicitly; no application connection is inherited")
        policy = json.loads(args.policy.read_text())
        if args.report.exists():
            raise ValueError("Report path already exists; choose a new evidence path")
        if args.action == "restore":
            if not args.expected_database or not args.expected_cluster or not args.expected_sha256:
                raise ValueError("Restore requires the provisioned database/cluster and reviewed archive SHA256")
            validate_destination_url(dsn, args.expected_database)
        elif args.archive.exists():
            raise ValueError("Archive path already exists; export never overwrites")
        # Open once so validation and restore consume the same file descriptor.
        with args.archive.open("rb" if args.action == "restore" else "xb") as archive:
            if args.action == "restore":
                digest = hashlib.file_digest(archive, "sha256").hexdigest()
                if digest != args.expected_sha256:
                    raise ValueError("Archive SHA256 does not match the reviewed copy")
                archive.seek(0)
            conn = await asyncpg.connect(dsn)
            try:
                if args.action == "export":
                    report = await export_snapshot(conn, policy, archive)
                else:
                    report = await restore_snapshot(
                        conn, policy, archive, database=args.expected_database, cluster=args.expected_cluster
                    )
            finally:
                await conn.close()
        with args.report.open("x") as report_file:
            json.dump(report, report_file, indent=2)
        print("Snapshot " + args.action + " completed; private verification report written")

    try:
        asyncio.run(run())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Driver errors can contain values, passwords or SQL. Keep them out of logs.
        print(
            "Snapshot operation failed ("
            + type(error).__name__
            + "); preserve private evidence and inspect destination before retrying",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
