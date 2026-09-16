"""Fail-closed checks shared by API, workers and Beat in the dedicated profile."""

from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.services.runtime_security.provision import READ_ONLY, RUNTIME_ROLE, SHARED_TABLES


def validate_runtime_configuration(config: Settings = settings) -> None:
    if config.SINGLE_COMPANY and config.APP_ENV != "development" and not config.DEDICATED_RUNTIME:
        raise ValueError("Dedicated production requires DEDICATED_RUNTIME=true")
    if not config.DEDICATED_RUNTIME:
        return
    if not config.SINGLE_COMPANY or config.APP_ENV not in {"production", "staging"} or config.APP_DEBUG:
        raise ValueError("Dedicated runtime requires single-company production/staging with debug disabled")
    if config.DATABASE_URL_DIRECT or config.DATABASE_URL_DIRECT_SYNC:
        raise ValueError("Operator/direct database URLs must not be present in runtime")
    try:
        urls = [make_url(config.DATABASE_URL), make_url(config.DATABASE_URL_SYNC)]
    except Exception:
        raise ValueError("Invalid runtime database URL configuration") from None
    if any(u.username != RUNTIME_ROLE or not u.password for u in urls):
        raise ValueError("API and worker database URLs must use the dedicated runtime role")
    if any(u.host != "postgres" or u.port != 5432 or u.query for u in urls):
        raise ValueError("The supported dedicated profile uses private postgres:5432 without URL overrides")
    if (urls[0].host, urls[0].port, urls[0].database, urls[0].password) != (
        urls[1].host,
        urls[1].port,
        urls[1].database,
        urls[1].password,
    ):
        raise ValueError("API and worker database identities must match")
    if len(config.JWT_SECRET_KEY) < 48 or config.JWT_SECRET_KEY.startswith("change-me"):
        raise ValueError("A deployment-specific JWT key of at least 48 characters is required")
    try:
        Fernet(config.ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        raise ValueError("A valid retained encryption key is required") from None
    origins = config.cors_origins_list
    for origin in [*origins, config.FRONTEND_URL]:
        u = urlsplit(origin)
        if (
            u.scheme != "https"
            or not u.hostname
            or "*" in origin
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.path not in {"", "/"}
        ):
            raise ValueError("Dedicated origins must be explicit HTTPS origins")
    if len(origins) != 1 or origins[0].rstrip("/") != config.FRONTEND_URL.rstrip("/"):
        raise ValueError("Dedicated CORS must match the single frontend origin")
    callback = urlsplit(config.NETSUITE_OAUTH_REDIRECT_URI)
    origin = urlsplit(origins[0])
    if (
        (callback.scheme, callback.netloc, callback.path)
        != ("https", origin.netloc, "/api/v1/connections/netsuite/callback")
        or callback.query
        or callback.fragment
    ):
        raise ValueError("Dedicated OAuth callback must use the verified HTTPS company origin")
    if config.EMAIL_PROVIDER != "resend" or not config.EMAIL_API_KEY:
        raise ValueError("Dedicated invitations require a configured email provider; console logging is forbidden")
    for value in [config.REDIS_URL, config.CELERY_BROKER_URL, config.CELERY_RESULT_BACKEND]:
        u = urlsplit(value)
        if u.scheme not in {"redis", "rediss"} or not u.hostname:
            raise ValueError("API, worker and Beat require configured Redis")
        if u.scheme == "redis" and u.hostname != "redis":
            raise ValueError("Plain Redis is permitted only on the private Compose redis service")


async def validate_runtime_database(db: AsyncSession) -> None:
    """Verify genuine login authority, object coverage and the installed binding."""
    role = (
        (
            await db.execute(
                text("""SELECT session_user, current_user, rolsuper, rolbypassrls,
        rolcreatedb, rolcreaterole, rolreplication, oid FROM pg_roles WHERE rolname=current_user""")
            )
        )
        .mappings()
        .one()
    )
    if (
        role["session_user"] != RUNTIME_ROLE
        or role["current_user"] != RUNTIME_ROLE
        or any(role[k] for k in ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication"))
    ):
        raise ValueError("Runtime database login must be the nonprivileged installed role")
    if (
        await db.execute(
            text("SELECT EXISTS(SELECT FROM pg_auth_members WHERE member=:oid OR roleid=:oid)"), {"oid": role["oid"]}
        )
    ).scalar():
        raise ValueError("Runtime database role must not have memberships")
    binding = (
        (await db.execute(text("SELECT company_id, database_name FROM suite_runtime_meta.binding"))).mappings().one()
    )
    identity = (await db.execute(text("SELECT current_database(), public.get_current_tenant_id()"))).one()
    if tuple(identity) != (binding["database_name"], binding["company_id"]):
        raise ValueError("Runtime database/company context mismatch")
    tables = (
        (
            await db.execute(
                text("""SELECT c.relname, c.relkind::text kind, c.relrowsecurity,
        pg_has_role(current_user,c.relowner,'MEMBER') owns,
        has_table_privilege(c.oid,'TRUNCATE') truncates,
        has_table_privilege(c.oid,'TRIGGER') triggers,
        EXISTS(SELECT FROM pg_policy p WHERE p.polrelid=c.oid AND p.polname='suite_runtime_bound'
          AND NOT p.polpermissive AND (SELECT oid FROM pg_roles WHERE rolname=current_user)=ANY(p.polroles)) bound,
        has_table_privilege(c.oid,'SELECT') readable,
        ARRAY(SELECT p.polcmd::text FROM pg_policy p WHERE p.polrelid=c.oid AND p.polpermissive
          AND (0=ANY(p.polroles) OR (SELECT oid FROM pg_roles WHERE rolname=current_user)=ANY(p.polroles))) commands
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m','f')""")
            )
        )
        .mappings()
        .all()
    )
    for row in tables:
        if row["kind"] not in {"r", "p"}:
            raise ValueError("Runtime relation inventory changed; operator review is required")
        if row["owns"] or row["truncates"] or row["triggers"]:
            raise ValueError("Runtime has excessive table authority")
        if not row["readable"] or (row["relname"] not in READ_ONLY and (not row["relrowsecurity"] or not row["bound"])):
            raise ValueError("Runtime table inventory changed; operator provisioning is required")
        if row["relname"] not in READ_ONLY:
            required = {"r", "w"} if row["relname"] == "tenants" else {"r", "a", "w", "d"}
            if row["relname"] == "audit_events":
                required = {"r", "a"}
            if "*" not in row["commands"] and not required.issubset(row["commands"]):
                raise ValueError("Runtime RLS command coverage is incomplete")
    elevated = (
        await db.execute(
            text("""SELECT has_database_privilege(current_database(),'CREATE')
       OR has_database_privilege(current_database(),'TEMP') OR has_schema_privilege('public','CREATE')
       OR has_table_privilege('suite_runtime_meta.binding','INSERT,UPDATE,DELETE,TRUNCATE')
       OR EXISTS(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege(p.oid,'EXECUTE'))""")
        )
    ).scalar()
    if elevated:
        raise ValueError("Runtime has unauthorized schema, binding or function authority")
    for table in READ_ONLY:
        if (
            await db.execute(
                text("SELECT has_table_privilege(:table,'INSERT,UPDATE,DELETE,TRUNCATE')"), {"table": table}
            )
        ).scalar():
            raise ValueError("System catalogs must be read-only for runtime")
    for table in SHARED_TABLES:
        policies = (
            await db.execute(
                text(
                    "SELECT count(*) FROM pg_policy WHERE polrelid=CAST(:table AS regclass) "
                    "AND polname IN ('suite_runtime_update','suite_runtime_delete') AND NOT polpermissive"
                ),
                {"table": table},
            )
        ).scalar_one()
        if policies != 2:
            raise ValueError("Shared catalog write protection is incomplete")
