#!/usr/bin/env python3
"""Give the uat-smoke tenant two recon exceptions the planner abstains on, so the
ResolutionAgent runs and — with JEV_RECON_RESOLUTION_MODE=shadow on the target —
Jev's reading is recorded beside the LLM's as audit action ``recon.jev_comparison``.

Why a separate script and not the smoke harness: the smoke's one unmatched charge
carries an order reference, so the planner resolves it itself (create_and_apply)
and the agent never gets an item. Shadow mode needs planner ABSTENTIONS:

  * a $77 charge on a payout whose status is 'failed'         -> rule 7: needs_human
  * a $5,000 charge matched to a $4,925 deposit (1.5%, within the fuzzy tolerance,
    above the pinned $50 materiality; not fee-like, not rounding) -> rule 8: needs_human

Unlike the smoke, ``seed`` LEAVES its rows in place: the agent is a Celery tail that
runs after plan-resolutions returns, and the comparison rows are the whole point.
``cleanup`` removes everything this script created (seed rows by dedupe prefix,
the run and its cascade, audit rows by correlation id) and asserts zero residue.

Safety: the smoke's slug guard (refuses any tenant whose slug != uat-smoke), and LOGIN
ONLY — this script never registers a tenant, because registration seeds soul.md.
Flags and materiality it changes are snapshotted and restored by cleanup.

    export UAT_SMOKE_EMAIL=... UAT_SMOKE_PASSWORD=...        # ~/.hermes/.env
    backend/.venv/bin/python scripts/uat/jev_shadow_seed.py seed \\
        --backend-url https://api-staging.suitestudio.ai --database-url "$DATABASE_URL_DIRECT"
    # ... wait a minute for the agent tail, then:
    backend/.venv/bin/python scripts/uat/jev_shadow_seed.py report  --database-url ...
    backend/.venv/bin/python scripts/uat/jev_shadow_seed.py cleanup --run-id <id> --prefix <p> --restore-json '<restore>' --database-url ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recon_live_smoke import (  # noqa: E402
    STANDARD_UAT_SLUG,
    SmokeFailure,
    _connect,
    _dedupe_prefix,
    _eprint,
    assert_uat_tenant,
    resolve_tenant,
)

SEED_DATE = date(
    2099, 2, 15
)  # a different window from the smoke's, so the two never meet
RUN_FROM, RUN_TO = date(2099, 2, 10), date(2099, 2, 20)
FAILED_REF, MISMATCH_REF = "R900000011", "R900000012"
FAILED_AMOUNT = Decimal("77.00")
MISMATCH_CHARGE, MISMATCH_DEPOSIT = Decimal("5000.00"), Decimal("4925.00")
AGENT_FLAG = "recon_resolution_agent"


async def _login_only(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Log in to the EXISTING uat-smoke tenant. Never register: the smoke's
    provision_and_auth may register a fresh tenant, and registration seeds
    /tmp/workspace_storage/{tenant}/soul.md — a file this repo never writes without
    explicit operator consent. A missing tenant is a setup failure, not something
    this script repairs."""
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    if resp.status_code != 200:
        raise SmokeFailure(
            f"login failed: HTTP {resp.status_code} {resp.text[:200]} — the uat-smoke tenant must already exist"
        )
    return resp.json()["access_token"]


async def _snapshot(conn, tenant_id: str) -> dict:
    """What seed changes besides its own rows, so cleanup can put it back."""
    tid = uuid.UUID(tenant_id)
    flags = await conn.fetch(
        "SELECT flag_key, enabled FROM tenant_feature_flags WHERE tenant_id = $1 AND flag_key = ANY($2::text[])",
        tid,
        ["reconciliation", AGENT_FLAG],
    )
    cfg = await conn.fetchrow(
        "SELECT recon_materiality_abs, recon_materiality_pct FROM tenant_configs WHERE tenant_id = $1",
        tid,
    )
    return {
        "flags": {
            r["flag_key"]: r["enabled"] for r in flags
        },  # absent key = flag row did not exist
        "materiality": [
            str(cfg["recon_materiality_abs"]),
            str(cfg["recon_materiality_pct"]),
        ]
        if cfg
        else None,
    }


async def _restore(conn, tenant_id: str, snap: dict) -> None:
    tid = uuid.UUID(tenant_id)
    for flag in ("reconciliation", AGENT_FLAG):
        if flag in snap["flags"]:
            await conn.execute(
                "UPDATE tenant_feature_flags SET enabled = $3, updated_at = now() WHERE tenant_id = $1 AND flag_key = $2",
                tid, flag, snap["flags"][flag],
            )  # fmt: skip
        else:
            await conn.execute(
                "DELETE FROM tenant_feature_flags WHERE tenant_id = $1 AND flag_key = $2",
                tid,
                flag,
            )
    if snap["materiality"]:
        await conn.execute(
            "UPDATE tenant_configs SET recon_materiality_abs = $2, recon_materiality_pct = $3, updated_at = now() WHERE tenant_id = $1",
            tid, Decimal(snap["materiality"][0]) if snap["materiality"][0] != "None" else None,
            Decimal(snap["materiality"][1]) if snap["materiality"][1] != "None" else None,
        )  # fmt: skip


async def _pin_materiality(conn, tenant_id: str) -> None:
    n = await conn.execute(
        "UPDATE tenant_configs SET recon_materiality_abs = $2, recon_materiality_pct = $3, updated_at = now() WHERE tenant_id = $1",
        uuid.UUID(tenant_id), Decimal("50.00"), Decimal("0.0100"),
    )  # fmt: skip
    if not n.endswith(" 1"):
        raise SmokeFailure(
            "uat-smoke has no tenant_configs row; provisioning incomplete"
        )


def _prefix(stamp: str) -> str:
    return _dedupe_prefix(f"jev-{stamp}")


async def _ensure_flag(conn, tenant_id: str, flag: str) -> None:
    await conn.execute(
        """
        INSERT INTO tenant_feature_flags (id, tenant_id, flag_key, enabled, created_at, updated_at)
        VALUES ($1, $2, $3, true, now(), now())
        ON CONFLICT (tenant_id, flag_key) DO UPDATE SET enabled = true, updated_at = now()
        """,
        uuid.uuid4(),
        uuid.UUID(tenant_id),
        flag,
    )


async def _payout(conn, tid, prefix, stamp, tag, amount, status):
    pid = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO payouts
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           amount, fee_amount, net_amount, currency, status, arrival_date, created_at, updated_at)
        VALUES ($1, $2, $3, 'stripe', $4, NULL, NULL, $5, 0, $5, 'USD', $6, $7, now(), now())
        """,
        pid, tid, f"{prefix}-payout-{tag}", f"po_jev_{stamp}_{tag}", amount, status, SEED_DATE,
    )  # fmt: skip
    return pid


async def _charge(conn, tid, prefix, stamp, tag, payout_id, amount, ref):
    await conn.execute(
        """
        INSERT INTO payout_lines
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           payout_id, line_type, amount, fee, net, currency, description, related_order_id, created_at, updated_at)
        VALUES ($1, $2, $3, 'stripe', $4, NULL, NULL, $5, 'charge', $6, 0, $6, 'USD', $7, $8, now(), now())
        """,
        uuid.uuid4(), tid, f"{prefix}-charge-{tag}", f"ch_jev_{stamp}_{tag}", payout_id, amount,
        f"Framework Marketplace Order ID: {ref}-JEVSHDW", ref,
    )  # fmt: skip


async def _deposit(conn, tid, prefix, stamp, tag, amount, ref):
    await conn.execute(
        """
        INSERT INTO netsuite_postings
          (id, tenant_id, dedupe_key, source, source_id, subsidiary_id, raw_data,
           netsuite_internal_id, record_type, transaction_date, amount, currency,
           account_id, account_name, memo, related_payout_id, created_at, updated_at)
        VALUES ($1, $2, $3, 'netsuite', $4, NULL, NULL, $5, 'custdep', $6, $7, 'USD', NULL, NULL, $8, $9, now(), now())
        """,
        uuid.uuid4(), tid, f"{prefix}-deposit-{tag}", f"ns_jev_{stamp}_{tag}", f"9{stamp[-6:]}{tag[:1]}",
        SEED_DATE, amount, f"Customer Deposit for {ref}", ref,
    )  # fmt: skip


async def seed(args) -> int:
    email = os.environ.get("UAT_SMOKE_EMAIL", "uat-smoke@example.com")
    password = os.environ.get("UAT_SMOKE_PASSWORD")
    if not password:
        _eprint("UAT_SMOKE_PASSWORD is required")
        return 2
    stamp = time.strftime("%Y%m%d%H%M%S")
    prefix = _prefix(stamp)
    async with httpx.AsyncClient(base_url=args.backend_url, timeout=60) as client:
        token = await _login_only(client, email, password)
        tenant_id = await resolve_tenant(client, token)
        conn = await _connect(args.database_url)
        try:
            await assert_uat_tenant(conn, tenant_id, STANDARD_UAT_SLUG)
            snapshot = await _snapshot(conn, tenant_id)
            await _ensure_flag(conn, tenant_id, "reconciliation")
            await _ensure_flag(conn, tenant_id, AGENT_FLAG)
            await _pin_materiality(conn, tenant_id)
            tid = uuid.UUID(tenant_id)
            failed = await _payout(
                conn, tid, prefix, stamp, "failed", FAILED_AMOUNT, "failed"
            )
            await _charge(
                conn, tid, prefix, stamp, "failed", failed, FAILED_AMOUNT, FAILED_REF
            )
            paid = await _payout(
                conn, tid, prefix, stamp, "paid", MISMATCH_CHARGE, "paid"
            )
            await _charge(
                conn,
                tid,
                prefix,
                stamp,
                "mismatch",
                paid,
                MISMATCH_CHARGE,
                MISMATCH_REF,
            )
            await _deposit(
                conn, tid, prefix, stamp, "mismatch", MISMATCH_DEPOSIT, MISMATCH_REF
            )
            _eprint(
                f"[seed] prefix={prefix!r}: failed-payout charge ${FAILED_AMOUNT}, ${MISMATCH_CHARGE} vs ${MISMATCH_DEPOSIT}"
            )
        finally:
            await conn.close()

        auth = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/reconciliation/runs",
            headers=auth,
            json={
                "date_from": RUN_FROM.isoformat(),
                "date_to": RUN_TO.isoformat(),
                "match_level": "order",
            },
        )
        if resp.status_code != 201:
            raise SmokeFailure(f"create run: HTTP {resp.status_code} {resp.text[:300]}")
        run_id = resp.json()["run_id"]
        _eprint(
            f"[run] {run_id} matched={resp.json().get('matched_count')} unmatched={resp.json().get('unmatched_count')}"
        )
        resp = await client.post(
            f"/api/v1/reconciliation/runs/{run_id}/plan-resolutions", headers=auth
        )
        if resp.status_code not in (200, 201):
            raise SmokeFailure(
                f"plan-resolutions: HTTP {resp.status_code} {resp.text[:300]}"
            )
        _eprint(f"[plan] {json.dumps(resp.json())[:300]}")
    print(
        json.dumps(
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "prefix": prefix,
                "restore": snapshot,
            }
        )
    )
    _eprint("The agent tail runs asynchronously; give it a minute, then run `report`.")
    return 0


async def report(args) -> int:
    conn = await _connect(args.database_url)
    try:
        rows = await conn.fetch(
            """
            SELECT a.timestamp AS created_at, a.correlation_id, a.payload
            FROM audit_events a JOIN tenants t ON t.id = a.tenant_id
            WHERE t.slug = $1 AND a.action = 'recon.jev_comparison'
            ORDER BY a.timestamp DESC LIMIT 50
            """,
            STANDARD_UAT_SLUG,
        )
    finally:
        await conn.close()
    if not rows:
        print(
            "no recon.jev_comparison rows for uat-smoke yet — is JEV_RECON_RESOLUTION_MODE=shadow set and the tenant allow-listed on the target?"
        )
        return 1
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        print(
            f"{r['created_at']:%Y-%m-%d %H:%M:%S}  run={str(r['correlation_id'])[:8]}  "
            f"llm={p.get('llm_action')} ({p.get('llm_elapsed_ms')} ms)  jev={p.get('jev_action')} "
            f"conf={p.get('jev_confidence')} ({p.get('jev_elapsed_ms')} ms)  agree={p.get('agree')}  "
            f"decided_by={p.get('decided_by')}  error={p.get('jev_error')}"
        )
    return 0


async def cleanup(args) -> int:
    if not args.run_id or not args.prefix:
        _eprint(
            "cleanup needs --run-id and --prefix (both printed by `seed`); --restore-json puts flags/materiality back"
        )
        return 2
    conn = await _connect(args.database_url)
    try:
        slug = await conn.fetchval(
            "SELECT t.slug FROM reconciliation_runs r JOIN tenants t ON t.id = r.tenant_id WHERE r.id = $1",
            uuid.UUID(args.run_id),
        )
        if slug is None:
            _eprint("run not found (already cleaned?) — removing seed rows only")
        elif slug != STANDARD_UAT_SLUG:
            raise SmokeFailure(
                f"SAFETY ABORT: run belongs to tenant slug {slug!r}, not {STANDARD_UAT_SLUG!r}"
            )
        tenant_id = await conn.fetchval(
            "SELECT id FROM tenants WHERE slug = $1", STANDARD_UAT_SLUG
        )
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM audit_events WHERE tenant_id = $1 AND correlation_id = $2",
                tenant_id,
                args.run_id,
            )
            await conn.execute(
                "DELETE FROM reconciliation_runs WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                uuid.UUID(args.run_id),
            )
            for table in ("payout_lines", "netsuite_postings", "payouts"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = $1 AND dedupe_key LIKE $2",
                    tenant_id,
                    f"{args.prefix}%",
                )
        residue = 0
        for table in ("payout_lines", "netsuite_postings", "payouts"):
            residue += await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE tenant_id = $1 AND dedupe_key LIKE $2",
                tenant_id,
                f"{args.prefix}%",
            )
        residue += await conn.fetchval(
            "SELECT count(*) FROM reconciliation_runs WHERE id = $1",
            uuid.UUID(args.run_id),
        )
    finally:
        await conn.close()
    print(f"residue={residue}")
    return 0 if residue == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("command", choices=["seed", "report", "cleanup"])
    p.add_argument(
        "--database-url",
        required=True,
        help="the target's DIRECT url, never the pooler",
    )
    p.add_argument("--backend-url", default="https://api-staging.suitestudio.ai")
    p.add_argument("--run-id")
    p.add_argument("--prefix")
    p.add_argument(
        "--restore-json",
        help="the `restore` object printed by seed; puts flags and materiality back",
    )
    args = p.parse_args()
    try:
        return asyncio.run(
            {"seed": seed, "report": report, "cleanup": cleanup}[args.command](args)
        )
    except SmokeFailure as exc:
        _eprint(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
