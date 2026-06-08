# Recon live-smoke harness (`recon_live_smoke.py`)

Reusable, parameterized, **zero-residue** live-smoke for the reconciliation
write-path. Phase 3 of the UAT/review triad (CI seeded-tenant e2e = Phase 2).
It exercises `create run -> approve bucket -> verify HITL invariants` against a
**deployed backend** (HTTP) + the **real database** (asyncpg), then deletes
everything it created and asserts the tenant returns to baseline.

It catches the failures the CI e2e structurally cannot: migration-not-applied-to-
staging, nginx/gateway timeout, env/secret, RLS-policy, and image-skew.

Spec: `docs/superpowers/plans/2026-06-07-recon-live-smoke-phase3.md`.
Gate wiring: `.claude/rules/uat-review.md` (T2 post-deploy live smoke).

## What it does

1. **Provision/auth (idempotent).** `POST /api/v1/auth/register` for the UAT
   tenant on the first run; on "already exists" (HTTP 400 *or* 409) falls back to
   `POST /api/v1/auth/login`. Resolves `tenant_id` via `/api/v1/auth/me`. The
   admin role granted at register carries `recon.run`, so no separate JWT mint is
   needed (sidesteps per-env JWT-secret differences).
2. **HARD safety guard.** Before any write it asserts the *resolved* tenant's
   `slug == --uat-slug`. If not, it ABORTS non-zero and writes nothing. This is
   the single gate that makes the harness safe to point at a real backend.
3. **Seed (DB).** Inserts a tiny deterministic canonical set for the UAT tenant:
   a `Payout` + a `charge` `PayoutLine` whose description embeds `R900000001` +
   a matching `custdep` `NetsuitePosting` (`related_payout_id = R900000001`, equal
   `$100`) → exact deterministic match → `matches` bucket; plus one `charge` with
   no deposit (`R900000002`, `$77`) → `needs_review`. Every row's `dedupe_key` is
   prefixed `uat-smoke-<run-stamp>` for exact cleanup. Enables the
   `reconciliation` feature flag (default-off at register) and pins the tenant's
   materiality so bucketing is hermetic.
4. **Create run (live API).** `POST /api/v1/reconciliation/runs`
   (`match_level: order`) over the seed window.
5. **Verify (live API + DB).** Approves the `matches` bucket and asserts:
   - response has a `correlation_id` and `approved_count >= 1`;
   - `audit_events` by `correlation_id`: exactly `approved_count` ×`recon.approve`
     + exactly 1 ×`recon.bulk_approve`;
   - **no NetSuite post** — `netsuite_postings`/`payouts` row counts unchanged;
   - run `total_variance` unchanged (approve is a status flip, not a re-compute);
   - approving the `needs_review` bucket returns **HTTP 400**.
   It deliberately does **NOT** `close_period` (a close on a shared tenant is
   sticky; the close path is covered deterministically by the Phase 2 CI e2e).
6. **Cleanup (try/finally, always).** Deletes the run (CASCADE drops results) +
   audit rows by `correlation_id` + the recon `create_run` audit by `resource_id`
   + the seeded canonical rows by `dedupe_key` prefix. Then asserts **zero
   residue** (run / results / audit-by-corr / seed-row counts all 0). Cleanup runs
   even on a mid-flight failure; it only touches the tenant once the safety guard
   has passed.
7. **Report.** Prints a structured JSON summary to **stdout** (diagnostics go to
   stderr). Exits `0` only on full pass **and** verified zero residue; otherwise
   non-zero (`1` = failed invariant / non-zero residue, `2` = missing config).

## Run it

```bash
# Local docker (postgres on :5432, backend on :8000)
backend/.venv/bin/python scripts/uat/recon_live_smoke.py \
  --backend-url http://localhost:8000 \
  --database-url 'postgresql://postgres:postgres@localhost:5432/ecom_netsuite'
```

```bash
# Staging UAT tenant (T2 post-deploy gate). DB url = the target's DATABASE_URL_DIRECT
# (direct, NOT the transaction pooler). Supabase TLS-no-verify + statement_cache_size=0
# are applied automatically for remote hosts; override with UAT_DB_SSL=require|disable.
export UAT_SMOKE_EMAIL=...           # never on the CLI
export UAT_SMOKE_PASSWORD=...
backend/.venv/bin/python scripts/uat/recon_live_smoke.py \
  --backend-url https://api-staging.suitestudio.ai \
  --database-url "$DATABASE_URL_DIRECT" \
  --uat-slug uat-smoke
```

Run it twice back-to-back; both must exit `0` and nothing must accumulate (each
run uses a fresh `<run-stamp>` so seeds never collide on the
`(tenant_id, dedupe_key)` unique constraint).

## Parameters

| Flag / env | Default | Purpose |
|---|---|---|
| `--backend-url` / `UAT_BACKEND_URL` | `http://localhost:8000` | Deployed backend base URL. |
| `--database-url` / `DATABASE_URL_DIRECT` | — (required) | Target direct Postgres URL for seed/cleanup. |
| `--uat-slug` / `UAT_SLUG` | `uat-smoke` | UAT tenant slug marker — the hard safety guard. |
| `UAT_SMOKE_EMAIL` | `uat-smoke@example.com` | UAT admin email (provision/login). **Must be globally unique** — `authenticate()` resolves email globally, not per-tenant. |
| `UAT_SMOKE_PASSWORD` | local default *(localhost only)* | UAT admin password. **Required** for any non-local `--backend-url`; the local default is refused off localhost (fails closed). |
| `UAT_DB_SSL` | auto | Force `require` or `disable` TLS for the DB connection. |

**Secrets:** the DB URL and credentials come from arg/env only — never hard-coded,
never echoed. Load them from `~/.hermes/.env` or the repo `.env` for local use.

## Zero-residue scope (what is cleaned vs what persists)

Cleanup drives the **recon footprint of the UAT tenant to absolute zero** and
leaves the tenant itself (and its provisioning trail) intact. Concretely:

**Cleaned to absolute zero** (asserted after every run — targeted *and*
tenant-wide):

- `reconciliation_runs` for the tenant (`reconciliation_results` cascade-delete
  with the run; the harness also asserts `0` results for the tenant directly);
- recon-action `audit_events` for the tenant — **every** recon audit action the
  backend writes (`RECON_AUDIT_ACTIONS`: `recon.approve`, `recon.bulk_approve`,
  `recon.run`, `recon.pipeline_run`, `recon.sync_trigger`, `recon.close_period`);
- the seeded canonical rows (`payouts` / `payout_lines` / `netsuite_postings`)
  under this run's `uat-smoke-<run-stamp>-…` dedupe prefix.

The absolute backstop counts `reconciliation_runs` /
`reconciliation_results` / recon-action `audit_events` **tenant-wide** (not just
by the ids this run captured). That catches an orphaned run even when `run_id`
was never captured — e.g. `create_run` succeeded server-side but its response
didn't parse, so cleanup had no id to delete by. The backstop DELETE sweeps these
tenant-wide before the re-count so the invariant can actually reach zero.

**Persists by design** (the UAT tenant is a long-lived, disposable *fixture* — it
is re-used across runs, not re-created):

- the `tenants` row + its `tenant_configs`, `users`, roles, feature flags;
- the tenant's **own auth/provisioning audit trail** — `audit_events` with
  `category = 'auth'` (`tenant.register`, `user.login`, `user.login_failed`,
  `user.google_login`, `user.logout`, `user.switch_tenant`). These belong to the
  *tenant*, not to any single smoke run, so they are **explicitly excluded** from
  the recon-residue assertion and are **never deleted**. (A growing login trail on
  a disposable fixture tenant is expected and harmless.)

## Safety guarantees

- **Never touches a non-UAT tenant.** The guard compares the *resolved* tenant's
  slug to `--uat-slug`; if a stale email collision resolves login to a different
  tenant, the guard refuses (verified by the negative test:
  `--uat-slug not-the-uat-tenant` aborts before any seed).
- **The smoke email MUST be globally unique.** The backend's `authenticate()`
  resolves an email **globally**, not tenant-scoped (one `User` per email across
  the whole DB at registration time). So a reused email could in principle log in
  to a *different* tenant than intended. The slug guard still protects you (it
  aborts the instant the resolved tenant's slug ≠ `--uat-slug`, before any write),
  but a dedicated, globally-unique `UAT_SMOKE_EMAIL` removes the ambiguity up
  front — use one address reserved for this harness, never a shared/human inbox.
- **No default password off localhost.** A built-in local default password is
  permitted **only** when `--backend-url` points at `localhost`/`127.0.0.1`. For
  any non-local target, `UAT_SMOKE_PASSWORD` **must** be set, or the harness fails
  closed (`UAT_SMOKE_PASSWORD must be set for non-local targets`) before
  connecting to anything.
- **Never `close_period`.** The harness has no close path in routine smoke.
- **Cleanup is mandatory + verified.** A run that cannot prove zero residue exits
  non-zero and logs the orphan ids under `orphans` in the JSON summary. Cleanup
  runs even on a mid-flight failure, uses the cleanup context captured the instant
  each server call returned (so a verify failure can't orphan the approve audit),
  and always populates `residue` — even if a DELETE or the re-count itself raises
  — so the JSON output is always diagnosable.
- **No NetSuite mutation.** Approve is a DB status flip + per-line audit only; the
  harness asserts canonical row counts are unchanged.
- **Run it SERIALLY against a given UAT tenant.** The zero-residue *absolute
  backstop* deletes **all** recon runs/recon-audit for the UAT tenant (so an
  orphan with an uncaptured `run_id` can't survive). That is safe only because the
  UAT tenant is recon-empty at rest and runs one-at-a-time — two concurrent smokes
  on the **same** UAT tenant would clobber each other's in-flight run. For a
  post-deploy gate this is the norm; if you ever need parallelism, use a distinct
  `--uat-slug` per concurrent runner.

## Local validation status

Validated against local docker (`postgres:5432`, backend `:8000`): registers a
fresh `uat-smoke` tenant on first run, falls back to login on repeats, passes all
invariants, and verifies zero residue. Confirmed idempotent across repeated
back-to-back runs — the **absolute** tenant-wide invariants
(`reconciliation_runs` / `reconciliation_results` / recon-action `audit_events` /
seed-row counts) all stay `0`, while the tenant's `tenant_config` and its
`auth`-category provisioning trail persist by design (see *Zero-residue scope*).
Negative tests verified: wrong `--uat-slug` → SAFETY ABORT before any write
(exit 1); missing `--database-url` → exit 2; non-local `--backend-url` with no
`UAT_SMOKE_PASSWORD` → fail closed before connecting (exit 1).

## Notes / portability

- **Feature-flag cache (direct-SQL enable bypasses the in-process cache).** The
  harness enables the `reconciliation` feature flag via **direct SQL** (an UPDATE
  on `tenant_feature_flags`), which does **not** go through
  `feature_flag_service.set_flag()` — so it bypasses the backend's in-process flag
  cache (`feature_flag_service._FLAG_CACHE`, 60s TTL, invalidated only by
  `set_flag` / `clear_cache` *in-process*). This is a non-issue for the post-deploy
  gate: after a deploy the backend process is fresh (cold cache), so the first
  `require_feature('reconciliation')` check reads the just-written DB value. The
  only window where a stale value could bite is **back-to-back runs within 60s on a
  long-lived process that had already cached the flag as `disabled`** (e.g. a prior
  call hit the feature before the harness enabled it) — in that case restart the
  backend or wait out the 60s TTL. Routine deploy-gated runs never hit this.
- Self-contained: imports nothing from `app` or `tests/conftest`; only stdlib +
  `httpx` + `asyncpg` (both already in `backend/.venv`). A thin pytest can wrap a
  local run for CI.
- The direct DB connection role must be able to write the tenant's canonical /
  config / audit rows. Local docker uses the `postgres` superuser (RLS not forced)
  so no `SET LOCAL app.current_tenant_id` is needed; for Supabase use a direct
  connection role with equivalent write access. RLS-policy failures surfacing here
  are exactly the kind of staging-only bug this gate exists to catch.
