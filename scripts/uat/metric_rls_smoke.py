#!/usr/bin/env python3
"""Staging RLS smoke for migration 082 — metric_definitions WITH CHECK enforcement.

Migration 082 added `WITH CHECK (tenant_id = get_current_tenant_id())` to
metric_definitions so the DB rejects any write whose tenant_id is not the caller's own
active tenant context. Local Postgres + the deploy's ephemeral migration-check both run
under RLS-bypassing roles, so they prove the policy APPLIES but never that it ENFORCES.

GROUND TRUTH (staging, 2026-06-09): the backend's own connection role (`postgres` on
Supabase) ALSO bypasses RLS (BYPASSRLS attribute). Two consequences this script is
built around:

  1. Simply connecting with the app's DATABASE_URL cannot exercise the policy — so each
     probe runs `SET LOCAL ROLE <probe>` first (default `authenticated`, the non-bypass
     Supabase role that holds INSERT on the table). The effective role inside the
     transaction is what RLS evaluates, so the WITH CHECK genuinely fires. If the
     connection role isn't a member of the probe role, membership is GRANTed *inside the
     rolled-back transaction* (transient, zero residue).
  2. RLS on this table is defense-in-depth for non-bypass consumers ONLY. It does NOT
     constrain the live app's own traffic while the app runs as a bypass role —
     application-level tenant scoping (set_tenant_context + query filters) remains the
     primary isolation for app connections. This smoke proves the policy enforces for
     the roles RLS can constrain; it deliberately does not claim more.

What it does (everything inside transactions that are ROLLED BACK — zero residue):
  1. Pre-flights the probe role: must exist, must not bypass RLS (BYPASSRLS attribute
     OR superuser — superusers bypass RLS unconditionally, even under FORCE), and must
     be assumable (directly or via transient GRANT). Aborts INCONCLUSIVE otherwise.
  2. Cross-tenant write: SET context = tenant A, INSERT a row with tenant_id = B.
     PASS requires this to be REJECTED with the RLS error (SQLSTATE 42501 carrying the
     row-level-security message — plain privilege denial shares the SQLSTATE and is
     classified INCONCLUSIVE, not FAIL).
  3. Positive control: SET context = tenant A, INSERT a row with tenant_id = A.
     PASS requires this to SUCCEED — proving the INSERT is well-formed, so the
     cross-tenant rejection in (2) can only be the WITH CHECK (not FK / NOT NULL).

Run against STAGING (never local) — the backend's own direct connection string works
because the probe role does the enforcing, not the connection role. DATABASE_URL_DIRECT
is REQUIRED (no DATABASE_URL fallback: targeting must be deliberate). Set it to the
staging DIRECT DSN (port 5432, not the pooler; credentials from env/VM only, never
inline) and run:

  backend/.venv/bin/python scripts/uat/metric_rls_smoke.py

Env overrides:
  RLS_SET_ROLE     probe role assumed inside each transaction (default `authenticated`;
                   set EMPTY to disable SET ROLE — then the connection role itself must
                   be non-bypassing or the script aborts)
  RLS_CTX_TENANT   caller tenant uuid (default: disposable uat-smoke staging tenant)
  RLS_OTHER_TENANT cross-tenant target uuid (default: SYSTEM tenant — the specific
                   tenant→SYSTEM hole 082 closes; a PASS under a non-SYSTEM override
                   does not exercise that hole and says so)

Exit codes: 0 = PASS (policy enforces), 1 = FAIL (cross-tenant write allowed → 082 not
enforced, or same-tenant write RLS-rejected → policy too strict), 2 = INCONCLUSIVE
(bypassing role / unassumable probe role / bad config / privilege-denial 42501 / ANY
unexpected error — misconfiguration is never reported as FAIL).
"""

import asyncio
import os
import re
import ssl
import sys
import uuid

import asyncpg

SYSTEM_TENANT = "00000000-0000-0000-0000-000000000000"
UAT_SMOKE_TENANT = "90fb7ae5-fd4c-4248-8f82-189a474c7523"  # disposable staging fixture
RLS_VIOLATION = "42501"  # insufficient_privilege — shared by RLS rejections AND plain privilege denials
FK_VIOLATION = "23503"
DEFAULT_PROBE_ROLE = "authenticated"

# Superusers bypass RLS unconditionally (even table FORCE only constrains the owner),
# so a bypass check on rolbypassrls alone would let a superuser produce a false FAIL.
_BYPASSES_RLS_SQL = "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user"
_BYPASSES_RLS_BY_NAME_SQL = "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = $1"

# Unquoted-identifier shape only: anything else risks interpolating into SET ROLE/GRANT.
_ROLE_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_$]*$")

_INSERT_SQL = """
    INSERT INTO metric_definitions
        (id, tenant_id, key, display_name, definition, unit, source_kind)
    VALUES (gen_random_uuid(), $1::uuid, $2, 'RLS smoke', 'rls smoke', 'currency', 'suiteql')
"""


class RoleSwitchError(RuntimeError):
    """The probe role cannot be used to render a verdict (→ INCONCLUSIVE, exit 2)."""


def _dsn() -> str:
    raw = os.environ.get("DATABASE_URL_DIRECT")
    if not raw:
        print(
            "INCONCLUSIVE: set DATABASE_URL_DIRECT explicitly (there is deliberately no DATABASE_URL "
            "fallback — this smoke must target staging on purpose, never inherit a local/CI DSN)"
        )
        raise SystemExit(2)
    # asyncpg wants a plain libpq DSN, not SQLAlchemy's +asyncpg dialect.
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgresql://")


def _probe_role() -> str | None:
    """Role assumed via SET LOCAL ROLE inside each probe transaction (None = disabled)."""
    raw = os.environ.get("RLS_SET_ROLE", DEFAULT_PROBE_ROLE).strip()
    if not raw:
        return None
    if not _ROLE_IDENT_RE.fullmatch(raw):
        raise ValueError(f"RLS_SET_ROLE {raw!r} is not a plain lowercase role identifier — refusing to interpolate it")
    return raw


def _tenants() -> tuple[str, str]:
    """Caller-context + cross-target tenant ids, NORMALIZED to canonical uuid form.

    Normalize-before-interpolate (same as set_tenant_context): uuid.UUID accepts
    uppercase/braced/dashless/urn spellings, so both the equality guard and the
    SET LOCAL interpolation must see the canonical form, never the raw env string."""
    raw_ctx = os.environ.get("RLS_CTX_TENANT", UAT_SMOKE_TENANT)
    raw_other = os.environ.get("RLS_OTHER_TENANT", SYSTEM_TENANT)
    try:
        ctx = str(uuid.UUID(raw_ctx))
        other = str(uuid.UUID(raw_other))
    except ValueError as e:
        raise ValueError("RLS_CTX_TENANT / RLS_OTHER_TENANT must be valid UUIDs") from e
    if ctx == other:
        raise ValueError("caller and target tenant must differ")
    return ctx, other


def _is_rls_rejection(state: str | None, msg: str | None) -> bool:
    """True iff the error is the RLS WITH CHECK rejection — NOT a plain privilege denial.

    Both raise SQLSTATE 42501; only the message distinguishes them ('new row violates
    row-level security policy' vs 'permission denied for ...'). Message matching assumes
    the server's default English lc_messages (true for Supabase/CI/docker images here)."""
    return state == RLS_VIOLATION and "row-level security" in (msg or "")


def _pg_error_line(e: BaseException) -> str:
    return (str(e).splitlines() or [""])[0][:160]


def _verdict(
    *,
    x_ok: bool,
    x_state: str | None,
    s_ok: bool,
    s_state: str | None,
    x_msg: str | None = None,
    s_msg: str | None = None,
) -> tuple[int, str]:
    """Map the two probe outcomes (cross-tenant, same-tenant) to (exit_code, message)."""
    # An ACCEPTED cross-tenant write is unconditional proof of non-enforcement — it must
    # dominate every positive-control outcome (a typo'd ctx tenant can't bury the hole).
    if x_ok:
        return 1, "FAIL: cross-tenant write was ACCEPTED — migration 082 WITH CHECK is NOT enforced."
    if not s_ok:
        if s_state == FK_VIOLATION:
            return 2, "INCONCLUSIVE: caller tenant does not exist — set RLS_CTX_TENANT."
        if s_state == RLS_VIOLATION:
            if _is_rls_rejection(s_state, s_msg):
                return 1, "FAIL: the policy rejected a SAME-tenant write — WITH CHECK is too strict."
            return 2, (
                "INCONCLUSIVE: same-tenant INSERT denied with plain 42501 (privilege denial, not an RLS "
                "rejection) — the probe role likely lacks INSERT / schema USAGE; fix grants and re-run."
            )
        return 2, (
            "INCONCLUSIVE: the positive-control INSERT failed for an unexpected reason "
            "(insert may be malformed); cannot trust the cross-tenant result."
        )
    if not _is_rls_rejection(x_state, x_msg):
        if x_state == RLS_VIOLATION:
            return 2, (
                "INCONCLUSIVE: cross-tenant write denied with plain 42501 (privilege denial, not the RLS "
                "WITH CHECK) — yet the same-tenant INSERT succeeded; investigate grants before trusting this."
            )
        return 2, (
            f"INCONCLUSIVE: cross-tenant write was rejected, but with sqlstate {x_state} "
            f"(expected {RLS_VIOLATION} row-level-security). Investigate."
        )
    return 0, (
        "PASS: cross-tenant write rejected by RLS (42501); same-tenant write accepted. "
        "Migration 082 WITH CHECK enforces for non-bypass roles on this database."
    )


async def _preflight(conn: asyncpg.Connection, probe_role: str, conn_user: str) -> bool:
    """Verify the probe role can render a verdict. Returns needs_grant (membership via
    transient in-transaction GRANT). Raises RoleSwitchError when no path works."""
    bypasses = await conn.fetchval(_BYPASSES_RLS_BY_NAME_SQL, probe_role)
    if bypasses is None:
        raise RoleSwitchError(f"probe role {probe_role!r} does not exist on this database")
    if bypasses:
        raise RoleSwitchError(
            f"probe role {probe_role!r} bypasses RLS (BYPASSRLS or superuser) — it cannot exercise the policy"
        )

    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(f'SET LOCAL ROLE "{probe_role}"')
        return False  # connection role is already a member
    except asyncpg.PostgresError:
        pass  # fall through to the transient-GRANT path
    finally:
        await tr.rollback()

    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(f'GRANT "{probe_role}" TO "{conn_user}"')
        await conn.execute(f'SET LOCAL ROLE "{probe_role}"')
        return True
    except asyncpg.PostgresError as e:
        raise RoleSwitchError(
            f"cannot assume probe role {probe_role!r} (not a member; transient GRANT failed): {e}"
        ) from e
    finally:
        await tr.rollback()


async def _attempt(
    conn: asyncpg.Connection,
    ctx_tenant: str,
    target_tenant: str,
    probe_role: str | None,
    needs_grant: bool,
    conn_user: str,
):
    """INSERT one metric_definitions row as `ctx_tenant` with tenant_id=`target_tenant`,
    then ROLL BACK. Returns (ok, sqlstate, msg, effective_role). ok=True iff accepted.

    Role-switch problems raise RoleSwitchError instead of being folded into the probe
    result: a failed SET ROLE also raises SQLSTATE 42501, which must never be mistaken
    for the RLS rejection this smoke exists to observe."""
    tr = conn.transaction()
    await tr.start()
    try:
        if probe_role:
            try:
                if needs_grant:
                    await conn.execute(f'GRANT "{probe_role}" TO "{conn_user}"')
                await conn.execute(f'SET LOCAL ROLE "{probe_role}"')
            except asyncpg.PostgresError as e:
                raise RoleSwitchError(f"could not assume probe role {probe_role!r}: {e}") from e
        effective = await conn.fetchval("SELECT current_user")
        bypasses = await conn.fetchval(_BYPASSES_RLS_SQL)
        if bypasses:
            # Both INSERTs would be accepted → x_ok=True → a FALSE FAIL against a
            # perfectly correct migration. Abort instead of mis-diagnosing.
            raise RoleSwitchError(
                f"effective role {effective!r} bypasses RLS (BYPASSRLS or superuser) — "
                "the verdict would be a false FAIL"
            )
        try:
            # SET LOCAL cannot bind-param; ctx_tenant is uuid-normalized by _tenants() before use.
            await conn.execute(f"SET LOCAL app.current_tenant_id = '{ctx_tenant}'")
            await conn.execute(_INSERT_SQL, target_tenant, f"__rls_smoke_{uuid.uuid4().hex[:8]}")
            return True, None, None, effective
        except asyncpg.PostgresError as e:
            return False, getattr(e, "sqlstate", None), _pg_error_line(e), effective
    finally:
        await tr.rollback()  # zero residue regardless of outcome


async def main() -> int:
    try:
        probe_role = _probe_role()
        ctx_tenant, other_tenant = _tenants()
    except ValueError as e:
        print(f"INCONCLUSIVE: {e}")
        return 2

    conn = None
    try:
        sslctx = ssl.create_default_context()
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE
        conn = await asyncpg.connect(_dsn(), ssl=sslctx, statement_cache_size=0)

        conn_user = await conn.fetchval("SELECT current_user")
        conn_bypasses = await conn.fetchval(_BYPASSES_RLS_SQL)
        print(f"connected as role={conn_user!r}  bypasses_rls={conn_bypasses}")
        if probe_role is None and conn_bypasses:
            print(
                "INCONCLUSIVE: this role bypasses RLS and RLS_SET_ROLE is disabled — the WITH CHECK\n"
                "cannot be exercised. Use the default probe role (unset RLS_SET_ROLE) or a non-bypass role."
            )
            return 2
        needs_grant = False
        if probe_role:
            needs_grant = await _preflight(conn, probe_role, conn_user)
            print(f"probe role            : {probe_role!r} (membership via transient GRANT: {needs_grant})")
        print(f"caller context tenant : {ctx_tenant}")
        print(f"cross-tenant target   : {other_tenant}\n")

        x_ok, x_state, x_msg, effective = await _attempt(
            conn, ctx_tenant, other_tenant, probe_role, needs_grant, conn_user
        )
        s_ok, s_state, s_msg, _ = await _attempt(conn, ctx_tenant, ctx_tenant, probe_role, needs_grant, conn_user)

        print(f"effective role inside probes : {effective!r}")
        print(f"cross-tenant INSERT : accepted={x_ok} sqlstate={x_state} {('— ' + x_msg) if x_msg else ''}")
        print(f"same-tenant  INSERT : accepted={s_ok} sqlstate={s_state} {('— ' + s_msg) if s_msg else ''}\n")

        code, msg = _verdict(x_ok=x_ok, x_state=x_state, s_ok=s_ok, s_state=s_state, x_msg=x_msg, s_msg=s_msg)
        if code == 0 and other_tenant != SYSTEM_TENANT:
            msg += (
                " NOTE: cross-target overridden away from the SYSTEM tenant — the specific tenant→SYSTEM "
                "hole 082 closes was NOT exercised this run; re-run without RLS_OTHER_TENANT for that proof."
            )
        print(msg)
        return code
    except RoleSwitchError as e:
        print(f"INCONCLUSIVE: {e}")
        return 2
    except Exception as e:  # connect/TLS/DNS/unexpected DB errors are config problems, not verdicts
        print(f"INCONCLUSIVE: unexpected error ({type(e).__name__}): {_pg_error_line(e)}")
        return 2
    finally:
        if conn is not None:
            await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
