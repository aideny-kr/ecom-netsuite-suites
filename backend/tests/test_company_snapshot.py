"""Selective snapshots must fail closed and preserve exact rows on an offline copy."""

import importlib
import uuid
from io import BytesIO

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from tests.conftest import _test_db_url


@pytest_asyncio.fixture
async def snapshot_db():
    url = make_url(_test_db_url)
    assert url.host in {"localhost", "127.0.0.1"} and url.database == "ecom_netsuite_test"
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    admin = await asyncpg.connect(dsn)
    name = "snapshot_test_" + uuid.uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    conn = await asyncpg.connect(url.set(drivername="postgresql", database=name).render_as_string(hide_password=False))
    await conn.execute("""
        CREATE TABLE alembic_version (version_num text PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('synthetic');
        CREATE TABLE tenants (id uuid PRIMARY KEY, slug text UNIQUE NOT NULL);
        CREATE TABLE connections (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), secret bytea);
        CREATE TABLE cursor_states (id uuid PRIMARY KEY, connection_id uuid REFERENCES connections(id), cursor_value text);
        CREATE TABLE domain_knowledge_chunks (id uuid PRIMARY KEY, raw_text text);
        CREATE TABLE jobs (id uuid PRIMARY KEY, tenant_id uuid, payload jsonb, amount numeric(20,8));
    """)
    try:
        yield conn
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest_asyncio.fixture
async def snapshot_seed(snapshot_db):
    company, other, connection, foreign_connection, public_note = [uuid.uuid4() for _ in range(5)]
    for tid, slug in [(company, "selected"), (other, "foreign")]:
        await snapshot_db.execute("INSERT INTO tenants VALUES ($1,$2)", tid, slug)
    for cid, tid in [(connection, company), (foreign_connection, other)]:
        await snapshot_db.execute("INSERT INTO connections VALUES ($1,$2,$3)", cid, tid, b"encrypted\x00bytes")
        await snapshot_db.execute("INSERT INTO cursor_states VALUES ($1,$2,$3)", uuid.uuid4(), cid, "unchanged")
        await snapshot_db.execute(
            "INSERT INTO jobs VALUES ($1,$2,$3,1.23456789)", uuid.uuid4(), tid, '{"history":[1,2]}'
        )
    await snapshot_db.execute(
        "INSERT INTO domain_knowledge_chunks VALUES ($1,'reviewed public note'),($2,'foreign private note')",
        public_note,
        uuid.uuid4(),
    )
    policy = dict(
        version=1,
        tenant_id=str(company),
        tenant_slug="selected",
        schema_revision=["synthetic"],
        tables={
            "alembic_version": {"scope": "all"},
            "tenants": {"scope": "company"},
            "connections": {"scope": "tenant"},
            "jobs": {"scope": "tenant"},
            "cursor_states": {"scope": "connections"},
            "domain_knowledge_chunks": {"scope": "ids", "ids": [str(public_note)]},
        },
    )
    return policy


def module():
    return importlib.import_module("app.cli.company_snapshot")


async def test_snapshot_selects_one_company_and_reviewed_shared_rows(snapshot_db, snapshot_seed):
    out = BytesIO()
    report = await module().export_snapshot(snapshot_db, snapshot_seed, out)
    assert report["tables"]["tenants"]["rows"] == 1
    assert report["tables"]["connections"]["rows"] == 1
    assert report["tables"]["cursor_states"]["rows"] == 1
    assert report["tables"]["domain_knowledge_chunks"]["rows"] == 1
    assert report["tables"]["jobs"]["rows"] == 1
    assert await snapshot_db.fetchval("SELECT count(*) FROM tenants") == 2
    assert out.getvalue().startswith(b"\x1f\x8b")


@pytest.mark.parametrize("change", ["unknown_table", "wrong_company", "unsafe_shared", "missing_id", "wrong_schema"])
async def test_snapshot_refuses_unreviewed_scope(snapshot_db, snapshot_seed, change):
    if change == "unknown_table":
        await snapshot_db.execute("CREATE TABLE new_private_table (secret text)")
    elif change == "wrong_company":
        snapshot_seed["tenant_slug"] = "incorrect"
    elif change == "unsafe_shared":
        snapshot_seed["tables"]["jobs"] = {"scope": "all"}
    elif change == "missing_id":
        snapshot_seed["tables"]["domain_knowledge_chunks"]["ids"] = [str(uuid.uuid4())]
    else:
        snapshot_seed["schema_revision"] = ["wrong"]
    with pytest.raises(ValueError):
        await module().export_snapshot(snapshot_db, snapshot_seed, BytesIO())


async def test_export_keeps_one_read_only_snapshot_during_concurrent_source_change(
    snapshot_db, snapshot_seed, monkeypatch
):
    api = module()
    original = api.copy_table
    changed = False

    async def verify_read_only(conn, *args, **kwargs):
        nonlocal changed
        assert await conn.fetchval("SHOW transaction_read_only") == "on"
        assert await conn.fetchval("SHOW transaction_isolation") == "repeatable read"
        result = await original(conn, *args, **kwargs)
        if not changed:
            changed = True
            name = await conn.fetchval("SELECT current_database()")
            dsn = (
                make_url(_test_db_url).set(drivername="postgresql", database=name).render_as_string(hide_password=False)
            )
            writer = await asyncpg.connect(dsn)
            try:
                await writer.execute(
                    "INSERT INTO jobs VALUES ($1,$2,'{}',9)", uuid.uuid4(), uuid.UUID(snapshot_seed["tenant_id"])
                )
            finally:
                await writer.close()
        return result

    monkeypatch.setattr(api, "copy_table", verify_read_only)
    report = await api.export_snapshot(snapshot_db, snapshot_seed, BytesIO())
    assert report["tables"]["jobs"]["rows"] == 1
    assert (
        await snapshot_db.fetchval(
            "SELECT count(*) FROM jobs WHERE tenant_id=$1", uuid.UUID(snapshot_seed["tenant_id"])
        )
        == 2
    )


async def make_archive(conn, policy, monkeypatch):
    """Simulate an independently initialized source; actual two-cluster rehearsal is separate."""
    api = module()
    identity = await api.database_identity(conn)

    async def source_identity(_):
        return {**identity, "cluster": str(int(identity["cluster"]) + 1)}

    out = BytesIO()
    with monkeypatch.context() as patch:
        patch.setattr(api, "database_identity", source_identity)
        await api.export_snapshot(conn, policy, out)
    out.seek(0)
    return out, identity


async def empty_synthetic_target(conn):
    # This fixture is a freshly created scratch database with only synthetic rows.
    await conn.execute("TRUNCATE cursor_states,connections,tenants,jobs,domain_knowledge_chunks,alembic_version")


async def test_restore_verifies_rows_bytes_scope_and_rerun_refusal(snapshot_db, snapshot_seed, monkeypatch):
    api = module()
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    await empty_synthetic_target(snapshot_db)
    result = await api.restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert result["verified"] is True
    assert await snapshot_db.fetchval("SELECT secret FROM connections") == b"encrypted\x00bytes"
    assert await snapshot_db.fetchval("SELECT count(*) FROM tenants") == 1
    assert str(await snapshot_db.fetchval("SELECT amount FROM jobs")) == "1.23456789"
    before = await snapshot_db.fetchval("SELECT cursor_value FROM cursor_states")
    archive.seek(0)
    with pytest.raises(ValueError, match="empty"):
        await api.restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert await snapshot_db.fetchval("SELECT cursor_value FROM cursor_states") == before


@pytest.mark.parametrize(
    "problem",
    ["same_cluster", "wrong_cluster", "wrong_database", "foreign_key", "truncated", "checksum", "policy", "schema"],
)
async def test_failed_restore_never_commits_partial_data(snapshot_db, snapshot_seed, monkeypatch, problem):
    import gzip
    import json

    api = module()
    if problem == "foreign_key":
        await snapshot_db.execute("ALTER TABLE jobs ADD COLUMN connection_id uuid REFERENCES connections(id)")
        foreign = await snapshot_db.fetchval(
            "SELECT c.id FROM connections c JOIN tenants t ON t.id=c.tenant_id WHERE t.slug='foreign'"
        )
        await snapshot_db.execute(
            "UPDATE jobs SET connection_id=$1 WHERE tenant_id=$2", foreign, uuid.UUID(snapshot_seed["tenant_id"])
        )
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    frames = [json.loads(line) for line in gzip.decompress(archive.getvalue()).splitlines()]
    if problem == "same_cluster":
        frames[0]["header"]["source"]["cluster"] = identity["cluster"]
    elif problem == "wrong_cluster":
        identity["cluster"] = "0"
    elif problem == "wrong_database":
        identity["database"] = "wrong"
    elif problem == "truncated":
        frames = frames[:-2]
    elif problem == "checksum":
        next(frame["end_table"] for frame in frames if "end_table" in frame)["sha256"] = "0" * 64
    elif problem == "policy":
        frames[0]["header"]["policy"]["tenant_slug"] = "unreviewed"
    elif problem == "schema":
        frames[0]["header"]["schema"]["tables"]["jobs"]["columns"][0]["type"] = "text"
    archive = BytesIO(gzip.compress(b"\n".join(api.canonical(f) for f in frames) + b"\n"))
    await empty_synthetic_target(snapshot_db)
    with pytest.raises((ValueError, EOFError)):
        await api.restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert await snapshot_db.fetchval("SELECT count(*) FROM tenants") == 0
    assert await snapshot_db.fetchval("SELECT count(*) FROM alembic_version") == 0
    assert await snapshot_db.fetchval("SHOW session_replication_role") == "origin"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@remote.example/company_rehearsal",
        "postgresql://u:p@127.0.0.1/live",
        "postgresql:///company_rehearsal",
    ],
)
def test_restore_cli_refuses_remote_or_non_rehearsal_destinations(dsn):
    with pytest.raises(ValueError):
        module().validate_destination_url(dsn, "company_rehearsal")


def test_restore_cli_allows_explicit_loopback_rehearsal():
    module().validate_destination_url("postgresql://u:p@127.0.0.1:5439/company_rehearsal", "company_rehearsal")


async def test_restore_recomputes_and_verifies_stored_generated_columns(snapshot_db, snapshot_seed, monkeypatch):
    await snapshot_db.execute("ALTER TABLE jobs ADD COLUMN doubled numeric GENERATED ALWAYS AS (amount*2) STORED")
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    await empty_synthetic_target(snapshot_db)
    result = await module().restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert result["verified"]
    assert str(await snapshot_db.fetchval("SELECT doubled FROM jobs")) == "2.46913578"
    assert result["tables"]["jobs"]["full_sha256"]


async def test_snapshot_refuses_unreviewed_custom_binary_types(snapshot_db, snapshot_seed):
    await snapshot_db.execute("CREATE TYPE private_status AS ENUM ('ready')")
    await snapshot_db.execute("ALTER TABLE jobs ADD COLUMN state private_status")
    with pytest.raises(ValueError, match="type"):
        await module().export_snapshot(snapshot_db, snapshot_seed, BytesIO())


@pytest.mark.parametrize("partial_null", [False, True])
async def test_restore_checks_composite_match_full_and_preserves_circular_links(
    snapshot_db, snapshot_seed, monkeypatch, partial_null
):
    await snapshot_db.execute("""
        ALTER TABLE jobs ADD CONSTRAINT job_pair UNIQUE (id,tenant_id);
        ALTER TABLE jobs ADD COLUMN related_id uuid;
        ALTER TABLE jobs ADD COLUMN related_tenant uuid;
        ALTER TABLE jobs ADD CONSTRAINT related_job FOREIGN KEY (related_id,related_tenant)
            REFERENCES jobs(id,tenant_id) MATCH FULL;
    """)
    company = uuid.UUID(snapshot_seed["tenant_id"])
    first = await snapshot_db.fetchval("SELECT id FROM jobs WHERE tenant_id=$1", company)
    second = uuid.uuid4()
    await snapshot_db.execute(
        "INSERT INTO jobs(id,tenant_id,related_id,related_tenant) VALUES ($1,$2,$3,$2)", second, company, first
    )
    await snapshot_db.execute("UPDATE jobs SET related_id=$1,related_tenant=$2 WHERE id=$3", second, company, first)
    if partial_null:
        await snapshot_db.execute("SET session_replication_role=replica")
        await snapshot_db.execute("UPDATE jobs SET related_tenant=NULL WHERE id=$1", first)
        await snapshot_db.execute("SET session_replication_role=origin")
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    await empty_synthetic_target(snapshot_db)
    if partial_null:
        with pytest.raises(ValueError, match="foreign-key"):
            await module().restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
        assert await snapshot_db.fetchval("SELECT count(*) FROM jobs") == 0
    else:
        await module().restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
        assert await snapshot_db.fetchval("SELECT related_id FROM jobs WHERE id=$1", first) == second
        assert await snapshot_db.fetchval("SELECT related_id FROM jobs WHERE id=$1", second) == first


async def test_source_table_locks_precede_the_repeatable_read_snapshot(snapshot_db, snapshot_seed, monkeypatch):
    api = module()
    original = api.schema_inventory
    attempted = False
    blocked = False

    async def race(conn):
        nonlocal attempted, blocked
        if not attempted:
            attempted = True
            await conn.fetchval("SELECT 1")  # Fix this transaction's snapshot.
            name = await conn.fetchval("SELECT current_database()")
            dsn = (
                make_url(_test_db_url).set(drivername="postgresql", database=name).render_as_string(hide_password=False)
            )
            writer = await asyncpg.connect(dsn)
            try:
                await writer.execute("SET lock_timeout='100ms'")
                try:
                    await writer.execute("TRUNCATE jobs")
                except asyncpg.LockNotAvailableError:
                    blocked = True
            finally:
                await writer.close()
        return await original(conn)

    monkeypatch.setattr(api, "schema_inventory", race)
    report = await api.export_snapshot(snapshot_db, snapshot_seed, BytesIO())
    assert blocked and report["tables"]["jobs"]["rows"] == 1


@pytest.mark.parametrize("drift", ["always_trigger", "replica_trigger", "rule", "tenant_global", "oid", "empty_table"])
async def test_snapshot_refuses_unsafe_schema_drift(snapshot_db, snapshot_seed, drift):
    if drift in {"always_trigger", "replica_trigger"}:
        await snapshot_db.execute("""CREATE FUNCTION snapshot_trigger() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$;
            CREATE TRIGGER unsafe BEFORE INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION snapshot_trigger();""")
        mode = "ALWAYS" if drift == "always_trigger" else "REPLICA"
        await snapshot_db.execute("ALTER TABLE jobs ENABLE " + mode + " TRIGGER unsafe")
    elif drift == "rule":
        await snapshot_db.execute("CREATE RULE unsafe AS ON INSERT TO jobs DO ALSO NOTIFY unsafe_restore")
    elif drift == "tenant_global":
        await snapshot_db.execute("ALTER TABLE alembic_version ADD COLUMN tenant_id uuid")
    elif drift == "oid":
        await snapshot_db.execute("ALTER TABLE jobs ADD COLUMN local_object oid")
    else:
        await snapshot_db.execute("CREATE TABLE hidden_empty_table ()")
    with pytest.raises(ValueError):
        await module().export_snapshot(snapshot_db, snapshot_seed, BytesIO())


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@127.0.0.1:1,remote.example:5432/company_rehearsal",
        "postgresql://u:p@127.0.0.1:99999/company_rehearsal",
    ],
)
def test_restore_cli_rejects_multi_host_and_invalid_port(dsn):
    with pytest.raises(ValueError):
        module().validate_destination_url(dsn, "company_rehearsal")


async def test_export_rejects_oversized_frame_before_completion(snapshot_db, snapshot_seed, monkeypatch):
    api = module()
    monkeypatch.setattr(api, "MAX_FRAME_SIZE", 128, raising=False)
    with pytest.raises(ValueError, match="frame"):
        await api.export_snapshot(snapshot_db, snapshot_seed, BytesIO())


async def test_export_refuses_an_existing_caller_transaction(snapshot_db, snapshot_seed):
    async with snapshot_db.transaction(isolation="repeatable_read"):
        with pytest.raises(ValueError, match="transaction"):
            await module().export_snapshot(snapshot_db, snapshot_seed, BytesIO())


async def test_vector_values_round_trip(snapshot_db, snapshot_seed, monkeypatch):
    await snapshot_db.execute("CREATE EXTENSION vector")
    await snapshot_db.execute("ALTER TABLE jobs ADD COLUMN embedding vector(3)")
    await snapshot_db.execute("UPDATE jobs SET embedding='[1.25,-2,0.125]'::vector")
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    await empty_synthetic_target(snapshot_db)
    await module().restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert await snapshot_db.fetchval("SELECT embedding::text FROM jobs") == "[1.25,-2,0.125]"


async def test_restore_refuses_foreign_rows_even_with_consistent_archive_hashes(
    snapshot_db, snapshot_seed, monkeypatch
):
    api = module()
    original = api.selection

    def include_foreign(name, *args):
        return "TRUE" if name == "jobs" else original(name, *args)

    with monkeypatch.context() as patch:
        patch.setattr(api, "selection", include_foreign)
        archive, identity = await make_archive(snapshot_db, snapshot_seed, patch)
    await empty_synthetic_target(snapshot_db)
    with pytest.raises(ValueError, match="outside reviewed"):
        await api.restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert await snapshot_db.fetchval("SELECT count(*) FROM jobs") == 0


async def test_export_fails_when_rls_would_hide_company_rows(snapshot_db, snapshot_seed):
    role = "snapshot_reader_" + uuid.uuid4().hex
    await snapshot_db.execute(f'CREATE ROLE "{role}"')
    await snapshot_db.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
    await snapshot_db.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
    await snapshot_db.execute(f'GRANT EXECUTE ON FUNCTION pg_control_system() TO "{role}"')
    await snapshot_db.execute("ALTER TABLE connections ENABLE ROW LEVEL SECURITY")
    await snapshot_db.execute("CREATE POLICY hide_company ON connections USING (false)")
    try:
        await snapshot_db.execute(f'SET ROLE "{role}"')
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await module().export_snapshot(snapshot_db, snapshot_seed, BytesIO())
    finally:
        await snapshot_db.execute("RESET ROLE")
        await snapshot_db.execute(f'DROP OWNED BY "{role}"')
        await snapshot_db.execute(f'DROP ROLE "{role}"')


async def test_schema_records_database_and_column_collation(snapshot_db):
    await snapshot_db.execute('ALTER TABLE tenants ALTER COLUMN slug TYPE text COLLATE "C"')
    schema = await module().schema_inventory(snapshot_db)
    assert schema["database_locale"]["encoding"] == "UTF8"
    slug = next(c for c in schema["tables"]["tenants"]["columns"] if c["name"] == "slug")
    assert slug["collation"]["name"] == "C"


async def test_restore_locks_schema_before_validating_it(snapshot_db, snapshot_seed, monkeypatch):
    api = module()
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    await empty_synthetic_target(snapshot_db)
    original = api.schema_inventory
    blocked = False

    async def race(conn):
        nonlocal blocked
        result = await original(conn)
        dsn = (
            make_url(_test_db_url)
            .set(drivername="postgresql", database=identity["database"])
            .render_as_string(hide_password=False)
        )
        writer = await asyncpg.connect(dsn)
        try:
            await writer.execute("SET lock_timeout='100ms'")
            try:
                await writer.execute("ALTER TABLE jobs ADD COLUMN unreviewed text")
            except asyncpg.LockNotAvailableError:
                blocked = True
        finally:
            await writer.close()
        return result

    monkeypatch.setattr(api, "schema_inventory", race)
    await api.restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert blocked


async def test_restore_overrides_repeatable_read_database_default(snapshot_db, snapshot_seed, monkeypatch):
    api = module()
    archive, identity = await make_archive(snapshot_db, snapshot_seed, monkeypatch)
    await empty_synthetic_target(snapshot_db)
    await snapshot_db.execute("SET default_transaction_isolation='repeatable read'")
    original = api.database_identity

    async def race(conn):
        result = await original(conn)
        dsn = (
            make_url(_test_db_url)
            .set(drivername="postgresql", database=identity["database"])
            .render_as_string(hide_password=False)
        )
        writer = await asyncpg.connect(dsn)
        try:
            await writer.execute("ALTER TABLE jobs ADD COLUMN unreviewed text")
        finally:
            await writer.close()
        return result

    monkeypatch.setattr(api, "database_identity", race)
    with pytest.raises(ValueError, match="schema"):
        await api.restore_snapshot(snapshot_db, snapshot_seed, archive, **identity)
    assert await snapshot_db.fetchval("SELECT count(*) FROM jobs") == 0
