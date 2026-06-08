#!/usr/bin/env python3
"""Recon LIVE-SMOKE harness — zero-residue, never-touch-a-real-tenant.

Phase 3 of the UAT/review triad (see
``docs/superpowers/plans/2026-06-07-recon-live-smoke-phase3.md``). Drives the
reconciliation create -> approve -> verify write-path end-to-end against a
*deployed* backend (HTTP) + a *real* database (asyncpg), then cleans up and
asserts zero residue. Catches the failures the CI e2e structurally cannot:
migration-not-applied, gateway timeout, env/secret, RLS-policy, image skew.

Self-contained ON PURPOSE: it imports NOTHING from the ``app`` package or
``tests/conftest`` so it stays portable for CI and staging. Only stdlib +
``httpx`` + ``asyncpg``.

Safety invariants (non-negotiable, enforced in code):
  * Hard guard before ANY write: the resolved tenant's ``slug`` MUST equal the
    configured ``--uat-slug`` marker, else abort non-zero. Never operate on a
    non-UAT tenant.
  * Never ``close_period`` (a close on a shared tenant is sticky). Live smoke =
    create + approve + verify only.
  * Cleanup is mandatory + verified (try/finally + zero-residue assertion). A run
    that cannot verify zero residue exits non-zero and logs the orphan ids.
  * Secrets via env/arg only — never hard-coded, never echoed.

Exit code 0 == full pass AND verified zero residue. Anything else == non-zero,
with a structured JSON summary on stdout.

Usage (local docker):
  python scripts/uat/recon_live_smoke.py \
    --backend-url http://localhost:8000 \
    --database-url 'postgresql://postgres:postgres@localhost:5432/ecom_netsuite'

Credentials (never on the CLI):
  UAT_SMOKE_EMAIL     admin email for the UAT tenant   (default: uat-smoke@example.com)
  UAT_SMOKE_PASSWORD  admin password                   (default: a generated-safe local default)
  DATABASE_URL_DIRECT fallback for --database-url
  UAT_BACKEND_URL     fallback for --backend-url
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg
import httpx

# --------------------------------------------------------------------------- #
# Deterministic seed constants. Far-future dates so the seed can NEVER collide
# with real ingested data; OrderReconJob applies a +/-14d buffer around the run
# window, so both the run window and the seeded dates sit comfortably inside it.
# --------------------------------------------------------------------------- #
SEED_DATE = date(2099, 1, 15)
RUN_DATE_FROM = date(2099, 1, 10)
RUN_DATE_TO = date(2099, 1, 20)
CLOSE_PERIOD = "2099-01"  # NEVER sent — referenced only to document what we skip

MATCH_ORDER_REF = "R900000001"  # exact-match charge<->deposit
UNMATCHED_ORDER_REF = "R900000002"  # charge with no deposit -> needs_review

MATCH_AMOUNT = Decimal("100.00")
UNMATCHED_AMOUNT = Decimal("77.00")

# Pin materiality so bucketing is hermetic regardless of tenant config drift.
# The exact match has zero variance, so it lands in 'matches' independent of
# these; we pin anyway so the run is deterministic if the seed ever grows.
PIN_MATERIALITY_ABS = Decimal("50.00")
PIN_MATERIALITY_PCT = Decimal("0.0100")


# Local-only default password. NEVER used against a non-local backend (see the
# secret-hardening gate in run_smoke): a non-local target with no
# UAT_SMOKE_PASSWORD set fails closed.
_LOCAL_DEFAULT_PASSWORD = "UatSmoke!2099pw"


def _is_local_backend(backend_url: str) -> bool:
    """True only when the backend URL targets localhost/127.0.0.1.

    Gates whether the local default password may be used. Uses urlparse so a
    host like ``localhost.evil.com`` does NOT match (hostname == 'localhost'
    exactly), and a path/query containing 'localhost' can't spoof it.
    """
    from urllib.parse import urlparse

    host = (urlparse(backend_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _eprint(*args: Any) -> None:
    """Diagnostics go to stderr so stdout stays a clean JSON summary."""
    print(*args, file=sys.stderr, flush=True)


@dataclass
class SmokeResult:
    passed: bool = False
    zero_residue: bool = False
    backend_url: str = ""
    tenant_id: str | None = None
    tenant_slug: str | None = None
    run_id: str | None = None
    correlation_id: str | None = None
    approved_count: int | None = None
    approved_result_ids: list[str] = field(default_factory=list)
    run_stamp: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    residue: dict[str, int] = field(default_factory=dict)
    orphans: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "passed": self.passed,
                "zero_residue": self.zero_residue,
                "backend_url": self.backend_url,
                "tenant_id": self.tenant_id,
                "tenant_slug": self.tenant_slug,
                "run_id": self.run_id,
                "correlation_id": self.correlation_id,
                "approved_count": self.approved_count,
                "approved_result_ids": self.approved_result_ids,
                "run_stamp": self.run_stamp,
                "checks": self.checks,
                "residue": self.residue,
                "orphans": self.orphans,
                "error": self.error,
            },
            indent=2,
            default=str,
        )


class SmokeFailure(Exception):
    """Raised on any failed invariant — triggers cleanup + non-zero exit."""


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def _build_ssl(database_url: str) -> ssl.SSLContext | bool:
    """Supabase needs TLS without local CA verification; bare Postgres uses none.

    Heuristic: Supabase/pooler/cloud hosts use TLS (no-verify). localhost/127.0.0.1
    docker uses plaintext. Override with UAT_DB_SSL=require|disable if needed.
    """
    override = os.environ.get("UAT_DB_SSL", "").strip().lower()
    if override in ("disable", "off", "false", "0"):
        return False
    if override in ("require", "on", "true", "1"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    lowered = database_url.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered or "@postgres:" in lowered:
        return False
    # Default for anything that looks remote (Supabase et al): TLS, no verify.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _connect(database_url: str) -> asyncpg.Connection:
    # statement_cache_size=0 is required for Supabase's transaction pooler; it is
    # harmless against direct/local Postgres. Use it unconditionally for portability.
    return await asyncpg.connect(
        database_url,
        ssl=_build_ssl(database_url),
        statement_cache_size=0,
    )


# --------------------------------------------------------------------------- #
# Step 1 — provision/auth idempotently
# --------------------------------------------------------------------------- #
async def provision_and_auth(
    client: httpx.AsyncClient,
    *,
    uat_slug: str,
    email: str,
    password: str,
) -> str:
    """Register the UAT tenant (first run) or login (later runs). Returns access token.

    register -> 201 with access_token (fresh tenant + admin user, admin grants
    recon.run). On "already exists" fall back to login.

    NOTE: the backend's explicit slug/email pre-check raises ValueError, which the
    register endpoint maps to **HTTP 400** ("Tenant slug already exists" /
    "Email already registered"); only the rarer IntegrityError race maps to 409.
    So idempotency must treat BOTH a 400-with-"already"-detail AND a 409 as
    "tenant exists -> login". Other 400s (bad password/slug validation) stay fatal.
    """
    reg_payload = {
        "tenant_name": f"UAT Smoke {uat_slug}",
        "tenant_slug": uat_slug,
        "email": email,
        "password": password,
        "full_name": "UAT Smoke Admin",
    }
    resp = await client.post("/api/v1/auth/register", json=reg_payload)
    if resp.status_code == 201:
        _eprint("[auth] registered fresh UAT tenant")
        return resp.json()["access_token"]

    detail = _detail(resp)
    already_exists = resp.status_code == 409 or (
        resp.status_code == 400 and "already" in detail.lower()
    )
    if already_exists:
        _eprint(f"[auth] tenant exists ({resp.status_code}: {detail!r}) -> login")
        login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        if login.status_code != 200:
            raise SmokeFailure(
                f"login failed after register-said-exists: HTTP {login.status_code} "
                f"{login.text[:200]}"
            )
        return login.json()["access_token"]
    raise SmokeFailure(
        f"register returned unexpected HTTP {resp.status_code}: {resp.text[:300]}"
    )


def _detail(resp: httpx.Response) -> str:
    """Best-effort extraction of a FastAPI error 'detail' string."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text or ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else json.dumps(detail or "")


def _truncate_body(body: Any, limit: int = 300) -> str:
    """Render a response body for an error message, truncated, never raising."""
    try:
        text = json.dumps(body, default=str)
    except Exception:  # noqa: BLE001
        text = repr(body)
    return text[:limit]


def _json_or_fail(resp: httpx.Response, where: str) -> dict[str, Any]:
    """Parse a JSON object body or raise SmokeFailure echoing the offending body.

    Defensive parsing: a malformed/non-object response must fail CLEANLY (with
    cleanup context preserved on the result) rather than KeyError/TypeError out
    of the happy path.
    """
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise SmokeFailure(
            f"{where}: response was not valid JSON ({exc}): {resp.text[:300]!r}"
        ) from exc
    if not isinstance(body, dict):
        raise SmokeFailure(
            f"{where}: expected a JSON object, got {type(body).__name__}: "
            f"{_truncate_body(body)}"
        )
    return body


def _nested_int(body: dict[str, Any], path: tuple[str, ...], where: str) -> int:
    """Walk a nested dict path expecting an int leaf, or raise echoing the body.

    Replaces ``body["matches"]["count"]`` style access so a malformed response
    fails CLEANLY (with cleanup context) instead of KeyError/TypeError-ing.
    """
    node: Any = body
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise SmokeFailure(
                f"{where}: missing key {'.'.join(path)!r}: {_truncate_body(body)}"
            )
        node = node[key]
    if not isinstance(node, int):
        raise SmokeFailure(
            f"{where}: key {'.'.join(path)!r} is not an int "
            f"({type(node).__name__}): {_truncate_body(body)}"
        )
    return node


def _extract_result_ids(abody: dict[str, Any]) -> list[str]:
    """Best-effort capture of the approved result ids from the approve response.

    Pure diagnostics for the cleanup-context object — cleanup deletes by run_id +
    correlation_id, not by these ids, so a shape we don't recognise is non-fatal.
    """
    candidates = (
        abody.get("approved_result_ids")
        or abody.get("result_ids")
        or abody.get("approved_ids")
    )
    if isinstance(candidates, list):
        return [str(x) for x in candidates]
    return []


async def resolve_tenant(client: httpx.AsyncClient, token: str) -> str:
    """Resolve tenant_id from /auth/me (authoritative, server-side)."""
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    if resp.status_code != 200:
        raise SmokeFailure(
            f"/auth/me failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()["tenant_id"]


# --------------------------------------------------------------------------- #
# Step 2 — HARD safety guard
# --------------------------------------------------------------------------- #
async def assert_uat_tenant(
    conn: asyncpg.Connection, tenant_id: str, uat_slug: str
) -> str:
    """Abort unless the resolved tenant carries the UAT slug marker.

    This is the single gate that makes the harness safe to point at a real
    backend: we NEVER seed/mutate/cleanup against a tenant whose slug is not the
    configured UAT marker.
    """
    row = await conn.fetchrow(
        "SELECT slug FROM tenants WHERE id = $1", uuid.UUID(tenant_id)
    )
    if row is None:
        raise SmokeFailure(f"SAFETY ABORT: tenant {tenant_id} not found in DB")
    slug = row["slug"]
    if slug != uat_slug:
        raise SmokeFailure(
            f"SAFETY ABORT: resolved tenant slug {slug!r} != UAT marker "
            f"{uat_slug!r}; refusing to operate on a non-UAT tenant"
        )
    _eprint(f"[guard] OK tenant {tenant_id} slug={slug!r} matches UAT marker")
    return slug


# --------------------------------------------------------------------------- #
# Step 3 — seed canonical rows + pin config
# --------------------------------------------------------------------------- #
async def ensure_reconciliation_flag(conn: asyncpg.Connection, tenant_id: str) -> None:
    """Enable the 'reconciliation' feature flag (default-off at register).

    Idempotent upsert on the (tenant_id, flag_key) unique constraint. The
    runs/buckets endpoints are gated by require_feature('reconciliation').
    """
    await conn.execute(
        """
        INSERT INTO tenant_feature_flags (id, tenant_id, flag_key, enabled, created_at, updated_at)
        VALUES (gen_random_uuid(), $1, 'reconciliation', true, now(), now())
        ON CONFLICT (tenant_id, flag_key)
        DO UPDATE SET enabled = true, updated_at = now()
        """,
        uuid.UUID(tenant_id),
    )


async def pin_materiality(conn: asyncpg.Connection, tenant_id: str) -> None:
    """Pin the tenant's recon materiality so bucketing is hermetic.

    register_tenant() always creates a TenantConfig row, so in practice this is
    an UPDATE. We use INSERT ... ON CONFLICT (tenant_id) DO UPDATE anyway as a
    belt-and-suspenders: if the config row is ever missing (manual tenant, or a
    register variant that skips it), bucketing stays hermetic instead of silently
    using the column defaults. ``tenant_id`` is the unique key on tenant_configs
    (confirmed in app/models/tenant.py — TenantConfig.tenant_id unique=True).
    """
    await conn.execute(
        """
        INSERT INTO tenant_configs
          (id, tenant_id, recon_materiality_abs, recon_materiality_pct,
           created_at, updated_at)
        VALUES (gen_random_uuid(), $1, $2, $3, now(), now())
        ON CONFLICT (tenant_id)
        DO UPDATE SET recon_materiality_abs = EXCLUDED.recon_materiality_abs,
                      recon_materiality_pct = EXCLUDED.recon_materiality_pct,
                      updated_at = now()
        """,
        uuid.UUID(tenant_id),
        PIN_MATERIALITY_ABS,
        PIN_MATERIALITY_PCT,
    )


async def seed_canonical(
    conn: asyncpg.Connection, tenant_id: str, run_stamp: str
) -> dict[str, str]:
    """Insert a tiny deterministic canonical set tagged with the run-stamp prefix.

    Produces:
      * 1 Payout
      * PayoutLine charge #1 (R900000001, $100) + NetsuitePosting custdep
        (related_payout_id=R900000001, $100) -> exact deterministic match -> 'matches'
      * PayoutLine charge #2 (R900000002, $77) with NO deposit -> 'needs_review'

    Every dedupe_key is prefixed 'uat-smoke-<run-stamp>' for exact cleanup. The
    run-stamp makes each run's seed unique so repeated/concurrent runs never
    collide on the (tenant_id, dedupe_key) unique constraint.
    """
    prefix = _dedupe_prefix(run_stamp)
    tid = uuid.UUID(tenant_id)

    payout_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO payouts
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           amount, fee_amount, net_amount, currency, status, arrival_date,
           created_at, updated_at)
        VALUES
          ($1, $2, $3, 'stripe', $4, NULL, NULL,
           $5, 0, $5, 'USD', 'paid', $6, now(), now())
        """,
        payout_id,
        tid,
        f"{prefix}-payout",
        f"po_{run_stamp}",
        MATCH_AMOUNT,
        SEED_DATE,
    )

    charge1_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO payout_lines
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           payout_id, line_type, amount, fee, net, currency, description,
           related_order_id, created_at, updated_at)
        VALUES
          ($1, $2, $3, 'stripe', $4, NULL, NULL,
           $5, 'charge', $6, 0, $6, 'USD', $7, $8, now(), now())
        """,
        charge1_id,
        tid,
        f"{prefix}-charge-match",
        f"ch_{run_stamp}_1",
        payout_id,
        MATCH_AMOUNT,
        f"Framework Marketplace Order ID: {MATCH_ORDER_REF}-XU9EPZPD",
        MATCH_ORDER_REF,
    )

    deposit_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO netsuite_postings
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           netsuite_internal_id, record_type, transaction_date, amount, currency,
           account_id, account_name, memo, related_payout_id, created_at, updated_at)
        VALUES
          ($1, $2, $3, 'netsuite', $4, NULL, NULL,
           $5, 'custdep', $6, $7, 'USD', NULL, NULL, $8, $9, now(), now())
        """,
        deposit_id,
        tid,
        f"{prefix}-deposit-match",
        f"ns_{run_stamp}_1",
        f"{run_stamp}001",
        SEED_DATE,
        MATCH_AMOUNT,
        f"Customer Deposit for {MATCH_ORDER_REF}",
        MATCH_ORDER_REF,
    )

    charge2_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO payout_lines
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           payout_id, line_type, amount, fee, net, currency, description,
           related_order_id, created_at, updated_at)
        VALUES
          ($1, $2, $3, 'stripe', $4, NULL, NULL,
           $5, 'charge', $6, 0, $6, 'USD', $7, $8, now(), now())
        """,
        charge2_id,
        tid,
        f"{prefix}-charge-unmatched",
        f"ch_{run_stamp}_2",
        payout_id,
        UNMATCHED_AMOUNT,
        f"Framework Marketplace Order ID: {UNMATCHED_ORDER_REF}-NODEPOS",
        UNMATCHED_ORDER_REF,
    )

    _eprint(
        f"[seed] payout=1 charges=2 (1 match $100 / 1 unmatched $77) deposit=1 "
        f"prefix={prefix!r}"
    )
    return {
        "payout_id": str(payout_id),
        "charge_match_id": str(charge1_id),
        "deposit_id": str(deposit_id),
        "charge_unmatched_id": str(charge2_id),
    }


def _dedupe_prefix(run_stamp: str) -> str:
    return f"uat-smoke-{run_stamp}"


# --------------------------------------------------------------------------- #
# Step 4 — create run (live API)
# --------------------------------------------------------------------------- #
async def create_run(client: httpx.AsyncClient, token: str, result: SmokeResult) -> str:
    resp = await client.post(
        "/api/v1/reconciliation/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "date_from": RUN_DATE_FROM.isoformat(),
            "date_to": RUN_DATE_TO.isoformat(),
            "match_level": "order",
        },
    )
    if resp.status_code != 201:
        raise SmokeFailure(
            f"create_run failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    body = _json_or_fail(resp, "create_run")
    run_id = body.get("run_id")
    if not run_id:
        raise SmokeFailure(
            f"create_run response missing 'run_id': {_truncate_body(body)}"
        )
    # CLEANUP-CONTEXT CAPTURE: store run_id the instant the server returned it,
    # BEFORE any verify assertion can raise. So if verify fails downstream, the
    # finally/cleanup still has run_id to DELETE by (no orphaned run).
    result.run_id = str(run_id)
    _eprint(
        f"[run] created {run_id} status={body.get('status')} "
        f"matched={body.get('matched_count')} unmatched={body.get('unmatched_count')} "
        f"variance={body.get('total_variance')}"
    )
    return str(run_id)


# --------------------------------------------------------------------------- #
# Step 5 — exercise + verify (live API + DB)
# --------------------------------------------------------------------------- #
async def verify(
    client: httpx.AsyncClient,
    conn: asyncpg.Connection,
    token: str,
    *,
    tenant_id: str,
    run_id: str,
    result: SmokeResult,
) -> str:
    """Exercise approve-bucket + assert HITL invariants. Returns correlation_id."""
    auth = {"Authorization": f"Bearer {token}"}
    tid = uuid.UUID(tenant_id)
    run_uuid = uuid.UUID(run_id)

    # --- buckets summary: expect >=1 'matches' and >=1 'needs_review' ---
    bresp = await client.get(
        f"/api/v1/reconciliation/runs/{run_id}/buckets", headers=auth
    )
    if bresp.status_code != 200:
        raise SmokeFailure(
            f"buckets summary failed: HTTP {bresp.status_code} {bresp.text[:200]}"
        )
    buckets = _json_or_fail(bresp, "buckets summary")
    matches_n = _nested_int(buckets, ("matches", "count"), "buckets summary")
    needs_n = _nested_int(buckets, ("needs_review", "count"), "buckets summary")
    result.checks["buckets_matches_count"] = matches_n
    result.checks["buckets_needs_review_count"] = needs_n
    if matches_n < 1:
        raise SmokeFailure(f"expected >=1 'matches' bucket row, got {matches_n}")
    if needs_n < 1:
        raise SmokeFailure(f"expected >=1 'needs_review' bucket row, got {needs_n}")

    # --- baseline: run total_variance + canonical row counts BEFORE approve ---
    variance_before = await conn.fetchval(
        "SELECT total_variance FROM reconciliation_runs WHERE id = $1 AND tenant_id = $2",
        run_uuid,
        tid,
    )
    postings_before = await conn.fetchval(
        "SELECT count(*) FROM netsuite_postings WHERE tenant_id = $1", tid
    )
    payouts_before = await conn.fetchval(
        "SELECT count(*) FROM payouts WHERE tenant_id = $1", tid
    )
    result.checks["run_total_variance_before"] = str(variance_before)

    # --- approve the 'matches' bucket ---
    aresp = await client.post(
        f"/api/v1/reconciliation/runs/{run_id}/approve-bucket",
        headers=auth,
        json={"bucket": "matches", "notes": "uat-smoke"},
    )
    if aresp.status_code != 200:
        raise SmokeFailure(
            f"approve-bucket(matches) failed: HTTP {aresp.status_code} {aresp.text[:300]}"
        )
    abody = _json_or_fail(aresp, "approve-bucket(matches)")
    correlation_id = abody.get("correlation_id")
    if not correlation_id or not isinstance(correlation_id, str):
        raise SmokeFailure(
            f"approve-bucket(matches) missing/invalid 'correlation_id': "
            f"{_truncate_body(abody)}"
        )
    approved_count = abody.get("approved_count")
    if not isinstance(approved_count, int):
        raise SmokeFailure(
            f"approve-bucket(matches) missing/invalid 'approved_count': "
            f"{_truncate_body(abody)}"
        )
    # CLEANUP-CONTEXT CAPTURE: store the correlation_id + approved_count + the
    # approved result ids the instant the server returned them, BEFORE any audit
    # assertion can raise. So a later verify failure still leaves cleanup with the
    # correlation_id to DELETE the approve-audit by (no orphaned audit trail).
    result.correlation_id = correlation_id
    result.approved_count = approved_count
    result.approved_result_ids = _extract_result_ids(abody)
    result.checks["approved_count"] = approved_count
    if approved_count < 1:
        raise SmokeFailure(
            f"approve-bucket approved_count={approved_count}, expected >=1"
        )

    # --- audit invariant: N x recon.approve + 1 x recon.bulk_approve, same corr id ---
    approve_rows = await conn.fetchval(
        """
        SELECT count(*) FROM audit_events
        WHERE tenant_id = $1 AND correlation_id = $2
          AND action = 'recon.approve' AND category = 'reconciliation'
        """,
        tid,
        correlation_id,
    )
    bulk_rows = await conn.fetchval(
        """
        SELECT count(*) FROM audit_events
        WHERE tenant_id = $1 AND correlation_id = $2
          AND action = 'recon.bulk_approve' AND category = 'reconciliation'
        """,
        tid,
        correlation_id,
    )
    result.checks["audit_recon_approve_count"] = approve_rows
    result.checks["audit_recon_bulk_approve_count"] = bulk_rows
    if approve_rows != approved_count:
        raise SmokeFailure(
            f"audit recon.approve rows={approve_rows} != approved_count={approved_count}"
        )
    if bulk_rows != 1:
        raise SmokeFailure(
            f"expected exactly 1 recon.bulk_approve audit row, got {bulk_rows}"
        )

    # --- HITL invariant: NO NetSuite post (canonical rows unchanged) ---
    postings_after = await conn.fetchval(
        "SELECT count(*) FROM netsuite_postings WHERE tenant_id = $1", tid
    )
    payouts_after = await conn.fetchval(
        "SELECT count(*) FROM payouts WHERE tenant_id = $1", tid
    )
    result.checks["netsuite_postings_unchanged"] = postings_before == postings_after
    result.checks["payouts_unchanged"] = payouts_before == payouts_after
    if postings_before != postings_after:
        raise SmokeFailure(
            f"NetSuite postings changed on approve ({postings_before} -> {postings_after}); "
            f"approve must NOT auto-post"
        )
    if payouts_before != payouts_after:
        raise SmokeFailure(
            f"payouts changed on approve ({payouts_before} -> {payouts_after})"
        )

    # --- HITL invariant: run total_variance unchanged (approve is a status flip) ---
    variance_after = await conn.fetchval(
        "SELECT total_variance FROM reconciliation_runs WHERE id = $1 AND tenant_id = $2",
        run_uuid,
        tid,
    )
    result.checks["run_total_variance_after"] = str(variance_after)
    # Null-safety: a completed run has a non-null total_variance. None on either
    # side means the run did not complete — None == None would pass vacuously, so
    # raise instead of blessing a non-completed run as "variance stable".
    if variance_before is None or variance_after is None:
        raise SmokeFailure(
            f"run total_variance is NULL (before={variance_before}, "
            f"after={variance_after}); a completed run must have a non-null "
            f"total_variance — the run likely did not complete"
        )
    result.checks["run_total_variance_unchanged"] = variance_before == variance_after
    if variance_before != variance_after:
        raise SmokeFailure(
            f"run total_variance changed on approve ({variance_before} -> {variance_after})"
        )

    # --- invariant: needs_review is NOT bulk-approvable -> HTTP 400 ---
    nresp = await client.post(
        f"/api/v1/reconciliation/runs/{run_id}/approve-bucket",
        headers=auth,
        json={"bucket": "needs_review", "notes": "uat-smoke-should-400"},
    )
    result.checks["needs_review_approve_status"] = nresp.status_code
    if nresp.status_code != 400:
        raise SmokeFailure(
            f"approve-bucket(needs_review) expected HTTP 400, got {nresp.status_code} {nresp.text[:200]}"
        )

    _eprint(
        f"[verify] PASS approved={approved_count} corr={correlation_id} "
        f"audit(approve={approve_rows}, bulk={bulk_rows}) no-post=OK variance-stable=OK "
        f"needs_review->400=OK"
    )
    return correlation_id


# --------------------------------------------------------------------------- #
# Step 6 — cleanup + zero-residue assertion (ALWAYS, try/finally)
# --------------------------------------------------------------------------- #
async def cleanup_and_verify(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    run_id: str | None,
    correlation_id: str | None,
    run_stamp: str,
    result: SmokeResult,
) -> None:
    """Delete everything this run created, then assert ABSOLUTE zero residue.

    Order matters:
      1. DELETE the run -> CASCADE drops reconciliation_results (ondelete=CASCADE).
      2. DELETE audit_events by correlation_id (bulk_approve summary + per-line
         recon.approve rows all share the batch correlation_id).
      3. ABSOLUTE-backstop sweep: DELETE any recon run + recon-action audit left
         on this disposable UAT tenant (covers the case where run_id /
         correlation_id were never captured because a response didn't parse).
      4. DELETE the seeded canonical rows by dedupe_key prefix (payout_lines,
         netsuite_postings, then payouts last because of the FK).

    Then re-count BOTH the targeted ids AND absolute tenant-wide invariants
    (reconciliation_runs / reconciliation_results / recon-action audit_events all
    == 0 for the tenant; seed rows == 0). The re-count ALWAYS runs even if a
    DELETE raised, and residue is ALWAYS populated (never {}) so the JSON output
    stays diagnosable. The tenant's own 'auth' provisioning trail is OUT of scope.
    """
    tid = uuid.UUID(tenant_id)
    prefix = _dedupe_prefix(run_stamp)
    like = f"{prefix}-%"

    cleanup_error: str | None = None
    try:
        # 1. run (CASCADE -> results)
        if run_id is not None:
            await conn.execute(
                "DELETE FROM reconciliation_runs WHERE id = $1 AND tenant_id = $2",
                uuid.UUID(run_id),
                tid,
            )

        # 2. audit by correlation_id (covers both the per-line recon.approve rows
        #    — written via resource_id but sharing this batch's correlation_id —
        #    and the recon.bulk_approve summary). Belt-and-suspenders: also clear
        #    any audit row referencing the run as resource_id (the create_run
        #    'recon.run' event).
        if correlation_id is not None:
            await conn.execute(
                "DELETE FROM audit_events WHERE tenant_id = $1 AND correlation_id = $2",
                tid,
                correlation_id,
            )
        if run_id is not None:
            await conn.execute(
                """
                DELETE FROM audit_events
                WHERE tenant_id = $1 AND resource_id = $2
                  AND category = 'reconciliation'
                """,
                tid,
                run_id,
            )

        # ABSOLUTE-backstop DELETE: even if run_id/correlation_id were NEVER
        # captured (e.g. create_run succeeded server-side but its response didn't
        # parse), sweep any recon run + recon-action audit left on this disposable
        # UAT tenant so the absolute invariants below can actually reach zero.
        # In-scope ONLY: recon runs (results CASCADE) + recon-action audit. The
        # tenant's own 'auth' provisioning trail is explicitly NOT touched.
        await conn.execute("DELETE FROM reconciliation_runs WHERE tenant_id = $1", tid)
        await conn.execute(
            """
            DELETE FROM audit_events
            WHERE tenant_id = $1
              AND action IN ('recon.approve', 'recon.bulk_approve',
                             'recon.run', 'recon.close_period')
            """,
            tid,
        )

        # 3. seeded canonical rows by dedupe_key prefix (payouts last: FK target)
        await conn.execute(
            "DELETE FROM payout_lines WHERE tenant_id = $1 AND dedupe_key LIKE $2",
            tid,
            like,
        )
        await conn.execute(
            "DELETE FROM netsuite_postings WHERE tenant_id = $1 AND dedupe_key LIKE $2",
            tid,
            like,
        )
        await conn.execute(
            "DELETE FROM payouts WHERE tenant_id = $1 AND dedupe_key LIKE $2",
            tid,
            like,
        )
    except Exception as exc:  # noqa: BLE001
        # A DELETE blew up. Do NOT escape with residue={} — fall through to the
        # re-count so the JSON output stays diagnosable. Record the error.
        cleanup_error = f"{type(exc).__name__}: {exc}"
        _eprint(f"[cleanup] DELETE error (still re-counting residue): {cleanup_error}")

    # --- zero-residue re-count (ALWAYS runs, even if a DELETE above raised) ---
    # Wrapped so that if the re-count ITSELF raises, we still record whatever we
    # gathered + the error instead of emitting zero_residue=False with residue={}.
    residue: dict[str, int] = {}
    try:
        # Targeted counts (by the ids/prefix THIS run created).
        if run_id is not None:
            residue["run"] = await conn.fetchval(
                "SELECT count(*) FROM reconciliation_runs WHERE id = $1 AND tenant_id = $2",
                uuid.UUID(run_id),
                tid,
            )
            residue["results"] = await conn.fetchval(
                "SELECT count(*) FROM reconciliation_results WHERE run_id = $1 AND tenant_id = $2",
                uuid.UUID(run_id),
                tid,
            )
        else:
            residue["run"] = 0
            residue["results"] = 0
        residue["audit_by_corr"] = (
            await conn.fetchval(
                "SELECT count(*) FROM audit_events WHERE tenant_id = $1 AND correlation_id = $2",
                tid,
                correlation_id,
            )
            if correlation_id is not None
            else 0
        )
        residue["seed_payout_lines"] = await conn.fetchval(
            "SELECT count(*) FROM payout_lines WHERE tenant_id = $1 AND dedupe_key LIKE $2",
            tid,
            like,
        )
        residue["seed_netsuite_postings"] = await conn.fetchval(
            "SELECT count(*) FROM netsuite_postings WHERE tenant_id = $1 AND dedupe_key LIKE $2",
            tid,
            like,
        )
        residue["seed_payouts"] = await conn.fetchval(
            "SELECT count(*) FROM payouts WHERE tenant_id = $1 AND dedupe_key LIKE $2",
            tid,
            like,
        )

        # ABSOLUTE backstop: invariants on the WHOLE UAT tenant, independent of
        # the ids we captured. Catches an orphaned run/results/audit even if
        # run_id or correlation_id was NEVER captured (e.g. create_run succeeded
        # server-side but the response didn't parse, so result.run_id is None).
        # The UAT tenant is disposable + recon-empty at baseline, so ANY recon
        # row is residue.
        #
        # SCOPE: recon runs/results + recon-action audit (recon.approve /
        # recon.bulk_approve / recon.run / recon.close_period) + seed rows must
        # hit absolute zero. The PERSISTENT tenant's OWN provisioning/auth trail
        # (category 'auth': tenant.register / user.login / user.login_failed /
        # ...) is intentionally OUT of scope — it belongs to the tenant, not this
        # run, so we neither delete it nor count it as residue.
        residue["abs_runs_for_tenant"] = await conn.fetchval(
            "SELECT count(*) FROM reconciliation_runs WHERE tenant_id = $1", tid
        )
        residue["abs_results_for_tenant"] = await conn.fetchval(
            "SELECT count(*) FROM reconciliation_results WHERE tenant_id = $1", tid
        )
        residue["abs_recon_audit_for_tenant"] = await conn.fetchval(
            """
            SELECT count(*) FROM audit_events
            WHERE tenant_id = $1
              AND action IN ('recon.approve', 'recon.bulk_approve',
                             'recon.run', 'recon.close_period')
            """,
            tid,
        )
    except Exception as exc:  # noqa: BLE001
        # The re-count itself failed. Populate residue with whatever we got + a
        # sentinel so the operator sees a non-empty, diagnosable residue dict and
        # zero_residue is (correctly) False below.
        recount_error = f"{type(exc).__name__}: {exc}"
        cleanup_error = (
            f"{cleanup_error} | re-count error: {recount_error}"
            if cleanup_error
            else f"re-count error: {recount_error}"
        )
        residue["recount_failed"] = 1
        _eprint(f"[cleanup] re-count error: {recount_error}")

    result.residue = residue
    if cleanup_error:
        result.error = (result.error or "") + f" | cleanup: {cleanup_error}"
    # zero_residue requires a clean cleanup AND zero counts. A non-empty residue
    # dict is guaranteed here (targeted + absolute counts, or the recount_failed
    # sentinel), so we never emit zero_residue=False with residue={}.
    total = sum(residue.values())
    result.zero_residue = total == 0 and cleanup_error is None
    if total != 0:
        result.orphans = {k: v for k, v in residue.items() if v}
        _eprint(f"[cleanup] NON-ZERO RESIDUE: {result.orphans}")
    elif cleanup_error is not None:
        _eprint("[cleanup] counts are zero but cleanup raised; zero_residue=False")
    else:
        _eprint("[cleanup] zero residue verified (targeted + absolute backstop)")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run_smoke(args: argparse.Namespace) -> SmokeResult:
    result = SmokeResult(backend_url=args.backend_url)
    # Full uuid4 hex (not [:12]) maximises dedupe-prefix entropy so concurrent /
    # repeated runs can never collide on the (tenant_id, dedupe_key) unique key.
    run_stamp = uuid.uuid4().hex
    result.run_stamp = run_stamp

    email = os.environ.get("UAT_SMOKE_EMAIL", "uat-smoke@example.com")
    # Secret hardening: a default password is ONLY permitted against a localhost
    # backend. For any non-local --backend-url, UAT_SMOKE_PASSWORD MUST be set
    # explicitly — never silently fall back to the local default (which would let
    # a stale/guessable credential reach a real deployment).
    password_env = os.environ.get("UAT_SMOKE_PASSWORD")
    if password_env:
        password = password_env
    elif _is_local_backend(args.backend_url):
        password = _LOCAL_DEFAULT_PASSWORD
    else:
        result.error = "UAT_SMOKE_PASSWORD must be set for non-local targets"
        _eprint(f"[FAIL] {result.error}")
        return result

    conn = await _connect(args.database_url)
    seeded = False
    try:
        async with httpx.AsyncClient(base_url=args.backend_url, timeout=60.0) as client:
            # 1. provision/auth
            token = await provision_and_auth(
                client, uat_slug=args.uat_slug, email=email, password=password
            )
            tenant_id = await resolve_tenant(client, token)
            result.tenant_id = tenant_id

            # 2. HARD safety guard (before ANY write)
            result.tenant_slug = await assert_uat_tenant(conn, tenant_id, args.uat_slug)

            # 3. seed (after the guard passes)
            await ensure_reconciliation_flag(conn, tenant_id)
            await pin_materiality(conn, tenant_id)
            await seed_canonical(conn, tenant_id, run_stamp)
            seeded = True

            # 4. create run (live API). create_run stamps result.run_id the
            # instant the server returns it, BEFORE any verify assertion runs.
            run_id = await create_run(client, token, result)

            # 5. exercise + verify. verify stamps result.correlation_id the
            # instant approve-bucket returns, BEFORE any audit assertion runs.
            await verify(
                client, conn, token, tenant_id=tenant_id, run_id=run_id, result=result
            )

        result.passed = True
    except SmokeFailure as exc:
        result.passed = False
        result.error = str(exc)
        _eprint(f"[FAIL] {exc}")
    except Exception as exc:  # noqa: BLE001 — report any unexpected error, still cleanup
        result.passed = False
        result.error = f"{type(exc).__name__}: {exc}"
        _eprint(f"[ERROR] {result.error}")
    finally:
        # Cleanup ALWAYS runs — even on mid-flight failure. Only attempt seed
        # cleanup if we passed the safety guard (tenant_slug set) so a failed
        # guard can never delete from a non-UAT tenant.
        if result.tenant_slug == args.uat_slug and result.tenant_id is not None:
            try:
                # Use the CAPTURED cleanup context (result.run_id /
                # result.correlation_id), stamped the instant each server call
                # returned — NOT happy-path locals. So a verify failure that
                # raised AFTER approve-bucket succeeded still deletes the approve
                # audit by its correlation_id (no orphaned audit trail). The
                # absolute backstop sweep covers the rarer case where even the
                # capture didn't happen (unparsed response).
                await cleanup_and_verify(
                    conn,
                    tenant_id=result.tenant_id,
                    run_id=result.run_id,
                    correlation_id=result.correlation_id,
                    run_stamp=run_stamp,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001
                result.zero_residue = False
                result.error = (result.error or "") + f" | cleanup error: {exc}"
                # Never leave residue empty if the whole cleanup escaped: surface
                # a sentinel so zero_residue=False is never paired with {}.
                if not result.residue:
                    result.residue = {"cleanup_unhandled_error": 1}
                _eprint(f"[cleanup ERROR] {exc}")
        else:
            # Guard never passed -> we never seeded -> nothing to clean. Mark
            # zero-residue True only if we genuinely did not seed.
            result.zero_residue = not seeded
            if seeded:
                _eprint(
                    "[cleanup] WARNING: seeded but guard state inconsistent; manual check"
                )
        await conn.close()

    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recon live-smoke harness (zero-residue, UAT-tenant-guarded).",
    )
    p.add_argument(
        "--backend-url",
        default=os.environ.get("UAT_BACKEND_URL", "http://localhost:8000"),
        help="Deployed backend base URL (default: http://localhost:8000).",
    )
    p.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL_DIRECT", ""),
        help="Target DATABASE_URL_DIRECT (Supabase: direct, not pooled). "
        "Falls back to env DATABASE_URL_DIRECT.",
    )
    p.add_argument(
        "--uat-slug",
        default=os.environ.get("UAT_SLUG", "uat-smoke"),
        help="UAT tenant slug marker — the hard safety guard (default: uat-smoke).",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.database_url:
        _eprint("ERROR: --database-url (or env DATABASE_URL_DIRECT) is required")
        print(json.dumps({"passed": False, "error": "missing database-url"}, indent=2))
        return 2

    result = await run_smoke(args)
    # stdout = clean structured JSON summary (the harness's machine-readable contract)
    print(result.to_json())

    ok = result.passed and result.zero_residue
    return 0 if ok else 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
