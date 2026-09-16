"""Real-login isolation tests on an exclusively owned, disposable database."""

import asyncio
import os
import secrets
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, settings
from app.schemas.auth import RegisterRequest
from app.services.company_bootstrap import bootstrap_company
from app.services.runtime_security.checks import validate_runtime_configuration, validate_runtime_database
from app.services.runtime_security.provision import RUNTIME_ROLE, SYSTEM, provision


def secure_config(**kwargs):
    values = dict(
        _env_file=None,
        SINGLE_COMPANY=True,
        DEDICATED_RUNTIME=True,
        APP_ENV="production",
        APP_DEBUG=False,
        DATABASE_URL="postgresql+asyncpg://suite_runtime:runtime-password@postgres:5432/test",
        DATABASE_URL_SYNC="postgresql://suite_runtime:runtime-password@postgres:5432/test",
        DATABASE_URL_DIRECT="",
        DATABASE_URL_DIRECT_SYNC="",
        JWT_SECRET_KEY="a" * 64,
        ENCRYPTION_KEY="dGVzdHRlc3R0ZXN0dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        FRONTEND_URL="https://company.example",
        CORS_ORIGINS="https://company.example",
        NETSUITE_OAUTH_REDIRECT_URI="https://company.example/api/v1/connections/netsuite/callback",
        EMAIL_PROVIDER="resend",
        EMAIL_API_KEY="synthetic-never-sent",
        REDIS_URL="redis://redis:6379/0",
        CELERY_BROKER_URL="redis://redis:6379/1",
        CELERY_RESULT_BACKEND="redis://redis:6379/2",
    )
    values.update(kwargs)
    return Settings(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"DEDICATED_RUNTIME": False},
        {"SINGLE_COMPANY": False},
        {"APP_DEBUG": True},
        {"DATABASE_URL_DIRECT": "postgresql://operator@postgres/db"},
        {"DATABASE_URL_SYNC": "postgresql://postgres:password@postgres/test"},
        {"DATABASE_URL_SYNC": "postgresql://suite_runtime:runtime-password@wrong:5432/test"},
        {"JWT_SECRET_KEY": "short"},
        {"ENCRYPTION_KEY": "invalid"},
        {"CORS_ORIGINS": "*"},
        {"CORS_ORIGINS": "https://company.example,https://other.example"},
        {"CORS_ORIGINS": "https://evil.example"},
        {"FRONTEND_URL": "http://company.example"},
        {"CELERY_BROKER_URL": "redis://public.example/1"},
        {"EMAIL_PROVIDER": "console"},
        {"EMAIL_API_KEY": ""},
        {"NETSUITE_OAUTH_REDIRECT_URI": "http://company.example/callback"},
    ],
)
def test_dedicated_config_refuses_unsafe_values(changes):
    with pytest.raises(ValueError):
        validate_runtime_configuration(secure_config(**changes))


def test_dedicated_config_accepts_private_profile():
    validate_runtime_configuration(secure_config())


@pytest_asyncio.fixture
async def installation(monkeypatch, tmp_path):
    url = make_url(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    if (
        settings.APP_ENV not in {"development", "test"}
        or url.host not in {"127.0.0.1", "localhost", "postgres"}
        or url.database not in {"ecom_netsuite", "ecom_netsuite_test"}
    ):
        pytest.fail("Runtime isolation tests require the disposable local/CI Postgres contract")
    control = await asyncpg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))
    name = "ecom_runtime_test_" + uuid.uuid4().hex[:12]
    op_url = url.set(database=name)
    runtime_password = secrets.token_urlsafe(36)
    engine = create_async_engine(op_url, echo=False)
    operator = None
    runtime_engine = None
    database_created = role_created = False
    try:
        # Serialize tests sharing a cluster without blocking the provisioning
        # transaction's separate advisory lock. Never adopt an existing role.
        await control.execute("SELECT pg_advisory_lock(731920260917)")
        assert not await control.fetchval("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=$1)", RUNTIME_ROLE), (
            "suite_runtime already exists: use a fresh disposable cluster; never remove an unverified role"
        )
        await control.execute(f'CREATE DATABASE "{name}"')
        database_created = True
        env = dict(
            os.environ,
            DATABASE_URL=op_url.render_as_string(hide_password=False),
            DATABASE_URL_DIRECT=op_url.render_as_string(hide_password=False),
            SINGLE_COMPANY="false",
            DEDICATED_RUNTIME="false",
            APP_DEBUG="false",
        )
        with (tmp_path / "migration.log").open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "upgrade",
                "head",
                cwd=Path(__file__).parents[1],
                env=env,
                stdout=log,
                stderr=log,
            )
            assert await process.wait() == 0, "Disposable migration failed; inspect private test log"
        monkeypatch.setattr(settings, "SINGLE_COMPANY", True)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            tenant, _ = await bootstrap_company(
                db,
                RegisterRequest(
                    tenant_name="Runtime fixture",
                    tenant_slug="runtime-fixture",
                    email="admin@runtime.example",
                    full_name="Fixture admin",
                    password="Synthetic-Only-Password7!",
                ),
            )
            company = tenant.id
        operator = await asyncpg.connect(op_url.set(drivername="postgresql").render_as_string(hide_password=False))
        cluster = await operator.fetchval("SELECT system_identifier::text FROM pg_control_system()")
        args = dict(database=name, cluster=cluster, company=company, password=runtime_password)
        preview = await provision(operator, **args)
        assert not preview["applied"]
        for overrides in ({"database": "wrong"}, {"cluster": "0"}, {"company": uuid.uuid4()}):
            with pytest.raises(ValueError):
                await provision(operator, **{**args, **overrides}, apply=True)
        assert not await control.fetchval("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=$1)", RUNTIME_ROLE)
        await operator.execute("CREATE TABLE policy_gap(id uuid,tenant_id uuid)")
        await operator.execute("ALTER TABLE policy_gap ENABLE ROW LEVEL SECURITY")
        for apply in (False, True):
            with pytest.raises(ValueError, match="coverage"):
                await provision(operator, **args, apply=apply)
        assert not await control.fetchval("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=$1)", RUNTIME_ROLE)
        await operator.execute("DROP TABLE policy_gap")
        for ddl, relation in (
            ("CREATE SEQUENCE unclassified", "SEQUENCE"),
            ("CREATE VIEW unclassified AS SELECT 1", "VIEW"),
        ):
            await operator.execute(ddl)
            with pytest.raises(ValueError, match="Unclassified relations"):
                await provision(operator, **args)
            await operator.execute(f"DROP {relation} unclassified")
        logging_before = await operator.fetchrow(
            "SELECT current_setting('log_statement'),current_setting('log_min_error_statement')"
        )
        role_created = (await provision(operator, **args, apply=True))["password_set"]
        assert role_created
        assert (
            await operator.fetchrow(
                "SELECT current_setting('log_statement'),current_setting('log_min_error_statement')"
            )
            == logging_before
        )
        # Rerun must preserve the original password and permissive-policy coverage.
        assert not (await provision(operator, **{**args, "password": secrets.token_urlsafe(36)}, apply=True))[
            "password_set"
        ]
        runtime_url = op_url.set(username=RUNTIME_ROLE, password=runtime_password)
        runtime_engine = create_async_engine(runtime_url, echo=False)
        yield dict(operator=operator, engine=runtime_engine, company=company, args=args, url=runtime_url)
    finally:
        if runtime_engine:
            await runtime_engine.dispose()
        await engine.dispose()
        if operator:
            await operator.close()
        try:
            if database_created:
                await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            if role_created:
                await control.execute(f"DROP ROLE IF EXISTS {RUNTIME_ROLE}")
        finally:
            # Closing also releases the cluster-wide test lock, even on refusal.
            await control.close()


async def test_real_runtime_boundaries_and_api(installation, monkeypatch):
    i = installation
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    op, company = i["operator"], i["company"]
    await op.execute(
        "INSERT INTO metric_definitions(tenant_id,key,display_name,definition,unit,source_kind) "
        "VALUES($1,'fixture','Shared','Shared fixture','currency','suiteql')",
        SYSTEM,
    )
    await op.execute(
        "INSERT INTO doc_chunks(id,tenant_id,source_path,title,chunk_index,content,token_count) "
        "VALUES($1,$2,'fixture','Shared',0,'Shared fixture',2)",
        uuid.uuid4(),
        SYSTEM,
    )
    factory = async_sessionmaker(i["engine"], expire_on_commit=False)
    await op.execute(
        "INSERT INTO domain_knowledge_chunks(id,source_uri,chunk_index,raw_text,token_count,source_type,is_deprecated) "
        "VALUES($1,'shared-fixture',0,'Protected curated rule',3,'expert_rules',false)",
        uuid.uuid4(),
    )
    await op.execute(
        "INSERT INTO domain_knowledge_chunks(id,source_uri,chunk_index,raw_text,token_count,source_type,partition_id,is_deprecated) "
        "VALUES($1,'bi/schema-docs/legacy',0,'Legacy unscoped schema',3,'bigquery_schema','bi/schema-docs',false)",
        uuid.uuid4(),
    )
    from app.core.encryption import encrypt_credentials
    from app.models.mcp_connector import McpConnector
    from app.services.bigquery_schema_seeder import seed_bigquery_schema

    schema = {"datasets": [{"dataset_id": "synthetic", "tables": [{"table_id": "orders", "columns": []}]}]}
    async with factory() as db:
        connector = McpConnector(
            tenant_id=company,
            provider="bigquery",
            label="Synthetic",
            server_url="https://unused.example",
            encrypted_credentials=encrypt_credentials({"service_account_json": {}, "project_id": "synthetic"}),
            metadata_json={},
        )
        db.add(connector)
        await db.commit()
        connector_id = connector.id
        for _ in range(2):
            assert await seed_bigquery_schema(db, company, schema) == 1
            await db.commit()
        assert (await db.execute(text("SELECT count(*) FROM domain_knowledge_chunks"))).scalar_one() == 2
        assert (
            await db.execute(
                text("UPDATE domain_knowledge_chunks SET raw_text='polluted' WHERE source_type='expert_rules'")
            )
        ).rowcount == 0
        await db.commit()
    assert await op.fetchval("SELECT count(*) FROM domain_knowledge_chunks") == 3
    async with factory() as db:
        await validate_runtime_database(db)
        assert (await db.execute(text("SELECT id FROM tenants"))).scalars().all() == [company]
        assert (await db.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
        # A real commit clears SET LOCAL; role startup context survives it.
        await db.execute(text("UPDATE tenant_configs SET brand_name='Allowed edit'"))
        await db.commit()
        assert (await db.execute(text("SELECT brand_name FROM tenant_configs"))).scalar_one() == "Allowed edit"
        await db.rollback()
        await validate_runtime_database(db)
    outsider = uuid.uuid4()
    outside_user = uuid.uuid4()
    member = uuid.uuid4()
    await op.execute(
        "INSERT INTO tenants(id,name,slug,plan,is_active,created_at,updated_at) VALUES($1,'Other','other','self_hosted',true,now(),now())",
        outsider,
    )
    await op.execute(
        "INSERT INTO tenant_configs(id,tenant_id,brand_name,created_at,updated_at) VALUES($1,$2,'Private',now(),now())",
        uuid.uuid4(),
        outsider,
    )
    for tenant_id, user_id, email in (
        (outsider, outside_user, "outside@runtime.example"),
        (company, member, "member@runtime.example"),
    ):
        await op.execute(
            "INSERT INTO users(id,tenant_id,email,hashed_password,full_name,is_active,created_at,updated_at) "
            "SELECT $1,$2,$3,hashed_password,'Fixture',true,now(),now() FROM users LIMIT 1",
            user_id,
            tenant_id,
            email,
        )
    # Provisioning itself must refuse a now-shared database, without altering it.
    with pytest.raises(ValueError, match="exactly"):
        await provision(op, **i["args"], apply=True)
    async with factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM tenants"))).scalar_one() == 1
        await db.execute(text(f"SET LOCAL app.current_tenant_id='{outsider}'"))
        assert (await db.execute(text("SELECT count(*) FROM tenant_configs"))).scalar_one() == 0
        await db.rollback()
    raw = await asyncpg.connect(i["url"].set(drivername="postgresql").render_as_string(hide_password=False))
    try:
        for table in ("metric_definitions", "doc_chunks"):
            assert await raw.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id=$1", SYSTEM) == 1
            assert await raw.execute(f"DELETE FROM {table} WHERE tenant_id=$1", SYSTEM) == "DELETE 0"
            assert (
                await raw.execute(f"UPDATE {table} SET tenant_id=$1 WHERE tenant_id=$2", company, SYSTEM) == "UPDATE 0"
            )
        for statement in [
            "SET ROLE postgres",
            "CREATE TABLE public.stolen(id int)",
            "CREATE TEMP TABLE stolen(id int)",
            "UPDATE permissions SET codename='stolen'",
            "DELETE FROM role_permissions",
            "TRUNCATE users",
            "ALTER TABLE users DISABLE ROW LEVEL SECURITY",
            "UPDATE suite_runtime_meta.binding SET company_id=gen_random_uuid()",
            "DELETE FROM audit_events",
        ]:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await raw.execute(statement)
        for source_type, source_uri in (
            ("expert_rules", "forged-global"),
            ("bigquery_schema", f"bi/schema-docs/{outsider}/forged"),
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await raw.execute(
                    "INSERT INTO domain_knowledge_chunks(id,source_uri,chunk_index,raw_text,token_count,source_type,partition_id,is_deprecated) "
                    "VALUES($1,$2,0,'forged',1,$3,'bi/schema-docs',false)",
                    uuid.uuid4(),
                    source_uri,
                    source_type,
                )
        await raw.execute(f"SET app.current_tenant_id='{SYSTEM}'")
        assert await raw.fetchval("SELECT count(*) FROM users") == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await raw.execute(
                "INSERT INTO tenant_configs(id,tenant_id,created_at,updated_at) VALUES($1,$2,now(),now())",
                uuid.uuid4(),
                outsider,
            )
    finally:
        await raw.close()
    from app.core.database import get_db
    from app.main import create_app

    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    app = create_app()

    async def runtime_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = runtime_db
    from app.api.v1 import mcp_connectors

    async def synthetic_schema(**kwargs):
        return schema

    monkeypatch.setattr(mcp_connectors, "discover_schema", synthetic_schema)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://company.example") as client:
        r = await client.post(
            "/api/v1/auth/login", json={"email": "admin@runtime.example", "password": "Synthetic-Only-Password7!"}
        )
        assert r.status_code == 200, r.text
        assert (
            "Secure" in r.headers["set-cookie"]
            and "HttpOnly" in r.headers["set-cookie"]
            and "SameSite=lax" in r.headers["set-cookie"]
        )
        headers = {"Authorization": "Bearer " + r.json()["access_token"]}
        selected = await client.put(
            f"/api/v1/mcp-connectors/bigquery/{connector_id}/tables",
            headers=headers,
            json={"selected_tables": {"synthetic": ["orders"]}},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["seeded_chunks"] == 1
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
        assert (await client.post("/api/v1/auth/refresh")).status_code == 200
        from app.core.security import create_access_token

        forged = create_access_token({"sub": str(outside_user), "tenant_id": str(outsider)})
        assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + forged})).status_code == 401
        member_token = create_access_token({"sub": str(member), "tenant_id": str(company)})
        assert (
            await client.get("/api/v1/invites", headers={"Authorization": "Bearer " + member_token})
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/auth/login", json={"email": "outside@runtime.example", "password": "Synthetic-Only-Password7!"}
            )
        ).status_code == 401
    # Both worker database paths must use the same role model after commits.
    from app.core import database

    monkeypatch.setattr(database, "_db_url", i["url"].render_as_string(hide_password=False))
    async with database.worker_async_session() as db:
        await validate_runtime_database(db)
        await db.commit()
        await validate_runtime_database(db)
    await op.execute(f"ALTER ROLE {RUNTIME_ROLE} BYPASSRLS")
    async with factory() as db:
        with pytest.raises(ValueError, match="nonprivileged"):
            await validate_runtime_database(db)
    await op.execute(f"ALTER ROLE {RUNTIME_ROLE} NOBYPASSRLS")
    # Real synchronous InstrumentedTask lifecycle for tenantless Beat sweeps.
    from sqlalchemy import create_engine

    from app.workers import base_task

    sync_engine = create_engine(i["url"].set(drivername="postgresql"))
    monkeypatch.setattr(base_task, "sync_engine", sync_engine)

    def worker_lifecycle():
        task = base_task.InstrumentedTask()
        task.name = "fixture.sweep"
        task.before_start("fixture-success", (), {})
        job_id = task._job_id
        task.on_success({"status": "ok"}, "fixture-success", (), {})
        with pytest.raises(ValueError, match="company"):
            task.before_start("fixture-rejected", (), {"tenant_id": str(outsider)})
        assert task._job_id is None
        task.on_failure(ValueError("rejected"), "fixture-rejected", (), {}, None)
        task.before_start("fixture-failure", (), {})
        task.on_failure(ValueError("synthetic failure"), "fixture-failure", (), {}, None)
        return job_id

    try:
        job_id = await asyncio.to_thread(worker_lifecycle)
        assert await op.fetchval("SELECT status FROM jobs WHERE id=$1", job_id) == "completed"
        assert (
            await op.fetchval("SELECT count(*) FROM jobs WHERE tenant_id=$1 AND job_type='fixture.sweep'", company) == 2
        )
        assert await op.fetchval("SELECT count(*) FROM audit_events WHERE job_id=$1", job_id) == 2
    finally:
        sync_engine.dispose()
    await op.execute("DROP POLICY users_tenant_isolation ON users")
    async with factory() as db:
        with pytest.raises(ValueError, match="coverage"):
            await validate_runtime_database(db)
    await op.execute("CREATE POLICY users_tenant_isolation ON users USING (tenant_id=get_current_tenant_id())")
    for ddl, relation in (
        ("CREATE SEQUENCE unclassified", "SEQUENCE"),
        ("CREATE VIEW unclassified AS SELECT 1", "VIEW"),
    ):
        await op.execute(ddl)
        async with factory() as db:
            with pytest.raises(ValueError, match="relation inventory"):
                await validate_runtime_database(db)
        await op.execute(f"DROP {relation} unclassified")
    # New tables stop startup until the operator classifies/provisions them.
    await op.execute("CREATE TABLE newly_added(id uuid, tenant_id uuid)")
    async with factory() as db:
        with pytest.raises(ValueError, match="table inventory"):
            await validate_runtime_database(db)


def test_operator_cli_does_not_print_database_errors(monkeypatch, capsys):
    from app.cli import runtime_role

    async def refuse(_args):
        raise RuntimeError("postgresql://operator:must-not-appear@private.example/db")

    monkeypatch.setattr(runtime_role, "run", refuse)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runtime_role",
            "--database",
            "test",
            "--cluster",
            "1",
            "--company",
            str(uuid.uuid4()),
            "--password-file",
            "/unused",
        ],
    )
    assert runtime_role.main() == 1
    captured = capsys.readouterr()
    assert "must-not-appear" not in captured.out + captured.err
    assert "Runtime provisioning refused" in captured.err


def test_dedicated_catalog_tasks_do_not_run_as_runtime(monkeypatch):
    from app.workers.tasks.metric_catalog_reseed import reseed_system_metrics_task
    from app.workers.tasks.oracle_skill_reseed import reseed_oracle_skills_task

    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    assert reseed_system_metrics_task() == {"status": "skipped", "reason": "operator_managed_catalog"}
    assert reseed_oracle_skills_task() == {"status": "skipped", "reason": "operator_managed_catalog"}
