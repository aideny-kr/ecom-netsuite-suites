# Suite Studio — open-source, single-company-per-deployment pivot

> Date: 2026-09-13 · Status: **plan, ready for operator decisions (section 2)** · Owner: Aiden
> Basis: 30-agent codebase audit + 34-agent market research (both adversarially verified), run against `main` at `430d575`.
> Tickets: ClickUp list **"Pivot · Open-Source Suite Studio"** (AI Research › AI-den).

---

## 0. The pivot in one paragraph

Suite Studio becomes an open-source repository anyone can clone and run locally or on their own cloud, installed by a coding agent (Claude Code / Codex / Cursor) that walks the operator through setup. Each deployment serves **one company** (own database, own compute). The business sells **deployment and reconciliation setup** (fixed fee) and **managed hosting** at USD 999/month per company at `<customer>.suitestudio.ai`, starting with Framework at `framework.suitestudio.ai`. The desktop app is dead. The five headline capabilities are: (a) user-authored scheduled agentic workflows, (b) ERP data-completeness checks with evidence-grounded fixes under human approval, (c) financial data exported to the customer's cloud folder, (d) customization-aware project scoping that writes tickets into the customer's PM tool, (e) large multi-threaded backfills into the ERP or data lake.

## 1. Verdict — is the idea good?

**Yes on direction, no on shape.** The research supports the thesis "software is cheap, deployment and architecture are expensive" specifically for this buyer:

| Signal | Evidence (see appendix A for sources) |
|---|---|
| The buyer already pays more than USD 999/month for less | NetSuite partners bill USD 125–300/hour; managed NetSuite admin sells at USD 2,500–25,000/month; a Celigo Shopify→NetSuite implementation is USD 30–75K plus a ~USD 14.7K/year median contract; close tooling (FloQast, Kolleno, Numeric) starts at USD 2,000+/month. |
| Per-company isolation is affordable | One `e2-standard-2` VM + in-compose Postgres/Redis + backups is ~USD 60–70/month infrastructure. Typical all-in COGS ≈ USD 195/month → ~80% gross margin at USD 999. |
| Comparable OSS-plus-hosting companies work | Medplum (Apache-2.0) charges USD 2,000/6,000 per month hosted; Frappe Cloud, Discourse, Ghost, Plausible all fund the company from hosting on a copyleft or permissive core. |
| The reconciliation pain is real | 50% of finance teams take 6+ days to close, 94% still close in Excel; Shopify/Stripe payout reconciliation is the #1 documented connector failure point. |

Four things must change for it to succeed. These are the load-bearing findings; everything below is built on them.

1. **Do not rip out multi-tenancy.** The codebase is uniformly multi-tenant (48 models carry `tenant_id`, 94 `set_tenant_context` call sites, 20 RLS migrations) but nothing assumes more than one tenant. "One company per deployment" is a **deployment topology**: bootstrap exactly one tenant at install time and delete the multi-tenant-only *surfaces* (platform admin, plans, billing). That is days of work. Removing `tenant_id` from the schema is weeks of work for no product benefit and throws away RLS as defense in depth.
2. **Reposition away from "AI chat over NetSuite".** Oracle is bundling that wedge for free: the NetSuite AI Connector (MCP) has no SKU, and NetSuite Next / Ask Oracle / SuiteAgents / AI bank matching / Intelligent Close Manager ship with 2026.1–2026.2 at no extra cost (Oracle pages were proxy-blocked during research; treat the timeline as high-probability, not verified). What Oracle will not bundle is **cross-system** reconciliation (Stripe/Shopify/warehouse → NetSuite), completeness checks grounded in the customer's own accounting settings, multi-threaded backfills, and hands-on deployment. Lead with those; build on the official AI Connector so Oracle's roadmap is an input.
3. **The open repo is a trust and sales asset, not the funnel.** Self-host → paid conversion across the OSS cohort is ~1%; no NetSuite MCP repository on GitHub exceeds 25 stars; the buyer is a controller or VP Finance who does not clone repositories. Revenue comes from direct sales of the setup package and hosting, with the repo removing the "lock-in / can we audit it" objection. Time-box community hours and measure inbound.
4. **USD 999 needs an envelope and a tier above it.** LLM spend is the only unbounded COGS line (a single 400K-line backfill is USD 1–5K of tokens at list price); ops hours are the founder's scarcest resource. Write the envelope into the contract and add a Hosted Plus tier so USD 999 does not become an unlimited retainer.

The pre-mortem's most likely failure is not any of the above: it is the **founder spending two quarters on refactor, playbook and OSS housekeeping while Framework remains the only revenue**. The plan therefore front-loads the smallest set of code changes that let a second company be deployed, and runs selling in parallel from week one.

## 2. Decisions to ratify (operator)

Each is recorded with the recommendation and the reason. Ratify or overturn; the tickets assume the recommendation.

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Tenancy | **Option A**: keep `tenant_id` + RLS; add `SINGLE_COMPANY` bootstrap; delete admin/plan/billing surfaces | Cheapest safe path; every Beat fan-out and RLS site degenerates correctly to one tenant. See §4.1. |
| D2 | License | **AGPL-3.0** for backend/frontend/workers; **Apache-2.0** for `suiteapp/`, `scripts/install/`, `docs/install/`, any SDK; `TRADEMARK.md` (Plausible/Ghost style); DCO sign-off; register "Suite Studio" (USPTO/EUIPO) after a clearance search against Oracle's Suite* family | Only AGPL/Apache can honestly be marketed as "open source"; AGPL forces hosted forks to publish modifications; the trademark, not the license, is what stopped AWS-style "Amazon Elasticsearch" confusion. If a hard legal ban on hosted resale matters more than the "open source" label, the alternative is FSL-1.1-Apache-2.0 ("fair source"). Decide before the first outside PR. |
| D3 | Positioning | Cross-system reconciliation + completeness checks + backfills + deployment, "for NetSuite" as a tagline, never "NetSuite" in a product or domain name | Oracle trademark guidelines forbid the mark inside product names; Oracle bundling collapses the chat wedge within 12–18 months. |
| D4 | Pricing | Self-host free · Setup package USD 15–35K fixed (anchor 25K) · Hosted USD 999/month with envelope (1 company, 10 users, 4 ops hours, USD 150 LLM at cost, per-job token budgets, BYOK option) · Hosted Plus USD 2,500–3,000 (SSO, SLA, 2 environments, close-week priority) · Ops retainer USD 2–5K hour bank · design partners 50% off for 12 months, price-locked 24 | Medplum USD 2,000/6,000; Frappe hybrid USD 1,000; Kolleno USD 2,490 floor. One metric from day one: **contribution margin per deployment**. |
| D5 | Hosting shape | One GCP project + one `e2-standard-2` VM per customer, unchanged compose stack incl. Postgres+Redis, Caddy per-host TLS, WAL-G/pgBackRest → GCS, OpenTofu module + small fleet CLI, public GHCR images pinned by digest, canary→waves upgrades | Cloud Run cannot host Postgres/Redis and costs ~4× for always-on services; GKE namespaces cost more than a VM at ≤50 customers with weaker isolation. |
| D6 | Installer | `AGENTS.md` (Codex/Cursor/Copilot) + `CLAUDE.md` first line `@AGENTS.md` + `docs/install/PLAYBOOK.md`; allow-listed idempotent `make`/`scripts/install/*.sh` verbs; secrets never enter the chat (`read -s` / localhost form / `op` / Secret Manager); committed deny rules + fail-closed PreToolUse hook; profiles `local` / `vm` / `managed` | Prose rules demonstrably fail (Claude Code issue #59094 leaked live keys despite a CLAUDE.md rule); enforcement must be hooks and permission rules. |
| D7 | NetSuite-side package | Ship a **minimal** Apache-2.0 SuiteApp (file-cabinet RESTlet only, `deploy.xml` fixed); the transaction-ops guard and mock-data RESTlet stay **out** of the public default; "NetSuite admin bootstrap" is a services line item | The guard hardcodes Framework custom fields (`custbody_fw_*`); the mock-data RESTlet is an unrestricted SuiteQL executor; the parent `deploy.xml` is malformed. |
| D8 | Sequencing | M1 (single-company mode + Framework on its own stack) → M2 (installer) → M3 (public cut) → M4 (fleet v1) → M5 (Settings/Connections) → M6 (selling points in revenue order b → e → a/c → d). Selling runs in parallel from week one. | Public repo is gated on the quickstart actually working (it is broken today) and on the single-company path being proven on Framework. |
| D9 | Existing tickets | Close the desktop track (B0–B4, Bet 1, OQ-044/045/046) and "cloud control-plane login + 14-day trial" | Dead by the pivot brief. |

## 3. What the audits established (facts, not opinions)

Every claim below was checked by a second agent reading the code; corrections from that pass are already applied.

### 3.1 Tenancy and single-company mode
- `Role`/`Permission` carry no `tenant_id`; RBAC is global and is already the boundary between users inside one company. Nothing to change (`backend/app/models/user.py:36-72`).
- Every Beat fan-out (`*_all` tasks) selects connections/tenants globally and dispatches per tenant; with one tenant they return 0–1 rows. Zero changes.
- **Day-15 outage**: `register_tenant` sets `plan='free'` and `plan_expires_at = now + 14 days` (`backend/app/services/auth_service.py:34-40`); `get_current_user` returns 403 "Plan expired" after that (`backend/app/core/dependencies.py:50-53`). A self-hosted install goes dark on day 15. Must be fixed before any install guide exists.
- `PLAN_LIMITS` is mostly dead: `mcp_tools`, `byok_ai`, `users`, `max_exports_per_day` have no call sites; `governance.py`'s 39 `requires_entitlement` keys are never read. The one live cap is `max_schedules=5` on the free plan, which directly fights selling point (a). Only two test files assert entitlement behaviour.
- `TenantWallet` is never constructed anywhere; chat billing is already a no-op. Deleting it is cleanup.
- Flag defaults that must flip to true for a single company: `reconciliation`, `celigo` (the transaction-ops router requires both), `recon_resolution_ui`. Keep as operator toggles: `drive_rag`, `plan_mode_enabled`, `recon_scheduled_runs`, `autonomous_recon`, `recon_resolution_agent`. The toggle surface that survives is `PATCH /api/v1/settings/features` (gated by `tenant.manage`, not superadmin).
- `admin.py` impersonation mints a valid access token for any active user with no re-auth, gated only by a boolean column. Delete outright, never hide (`backend/app/api/v1/admin.py:159-199`).
- Google login is optional by design (provider wrapper no-ops without a client id) but `login/page.tsx:153` mounts `<GoogleLogin>` unconditionally; with no provider context the library throws. Gate it.
- Route-level gates: most `require_feature` gates sit beside `require_permission`; the exceptions (`chat.py` create_session/send_message, `onboarding_netsuite_mcp_authorize`) are authenticated-user routes, not security holes.

### 3.2 Open-source readiness
- Full git history scan: **no real credentials**. Every hit is a fixture or an example in the vendored OWASP skill.
- Internal identifiers to scrub: staging IP `34.73.236.64` + ssh user (7 files), `ghcr.io/aideny-kr` (6 files incl. CI + prod compose), the live Google OAuth client id (`docker-compose.yml:42,107`), `staging.suitestudio.ai` (11 files), ~20 ClickUp ids, 40+ `/Users/aidenyi` paths (all in `docs/superpowers`).
- Customer coupling is wider than one file: `framework_defaults.py` (account, subsidiary map, a step UUID), `FrameworkIcon` used as the assistant avatar on two secondary surfaces (`frontend/src/components/chat/message-list.tsx:41-53`), `frameworkreporting.*` dataset names in `knowledge/golden_dataset/*.md`, the Framework tenant UUID as a default in `agent_benchmark_vs_mcp.py:425`, and account `6738075` in 66 files (≈83% tests/fixtures). **Solidus** is a first-class provider (`schemas/connection.py`, `solidus_sync`, migration 102, five prompt files) — but Solidus is an open-source commerce platform, so keep it as a generic connector and only drop the "Framework Solidus" copy.
- Licensing of what is bundled: Oracle skills are UPL-1.0 (GPL-compatible, redistributable); all backend/npm dependencies checked are permissive; `psycopg2-binary` is LGPL with linking exception (name it in NOTICE). The two desktop submodules are not checked out — deleting `desktop/` removes the question.
- README describes an architecture that does not run (three-tier router, specialist agents, circuit breaker); only `UnifiedAgent` is instantiated. Four specialist agent files are dead code.
- CI: `deploy.yml`, `rollback.yml`, `agent-benchmark.yml`, `pricing-soak.yml` need operator secrets and staging; keep `ci.yml` public, move the rest to a private ops repo. The vs-MCP benchmark gate in CLAUDE.md cannot run on external PRs; it becomes a maintainer-only nightly.

### 3.3 Install surface (what a fresh clone actually does today)
- `docker compose up` **never runs migrations** (`backend/entrypoint.sh` defers to CI); registration then silently creates an admin with zero roles.
- The dev compose **frontend service cannot boot**: it bind-mounts source over a production standalone image whose `CMD` is `node server.js`, which only exists in build output. The README quickstart is broken.
- `GOOGLE_CLIENT_ID`, `EMAIL_*`, `FRONTEND_URL` are read via bare `os.environ`, invisible to `config.py`.
- Without Redis, rate limiting and the JWT denylist fall back to memory silently, in every `APP_ENV`.
- Embeddings key absent → domain knowledge falls back to keyword search (not a silent no-op, corrected from the first audit). Oracle-skill and BigQuery schema seeding do not depend on it.
- NetSuite needs **two** integration records (REST `rest_webservices,restlets`; MCP `mcp`) plus an SDF deploy; the bundled suitecloud CLI is used only for customer-authored changesets, never for the parent SuiteApp. The parent `deploy.xml` nests `Objects/*` under `<configuration>` instead of `<objects>` (malformed), `transaction_ops_guard_url` has no write path (hand-edited DB rows), and the file-cabinet client defaults to numeric script id `3901` from one account.
- Health endpoints check only DB and Redis liveness; a real doctor must check migration head, seeded roles, provider keys, and the frontend build id.

### 3.4 Selling points — honest maturity
| Selling point | Exists | Missing | Maturity | Minimal demo build |
|---|---|---|---|---|
| (a) Scheduled workflows | 6-type allow-listed registry, LLM compiler with repair/clarify, Beat executor with budgets/reasons/retry-pause, pages; ran live on staging | No NetSuite/Celigo write steps (deliberate); no custom agent step | **shipped-live** | None — market as scheduled report/delivery automation |
| (b) Completeness check + HITL fix + learned autonomy | transaction_ops detects missing orders / amount / VAT differences, re-reads native subledger detail, enforces period-open and balanced-entry invariants, immutable proposals, guarded execution | Autonomy: envelope is dry-run only; learned rules are prompt text, not executable; no durable execution (`max_retries=0` everywhere, no `acks_late`); reject action exists since PR #193 (STATE.md is stale on this) | **built, HITL only** | Sell "evidence-grounded proposals a human approves"; add accounting-settings discovery step |
| (c) Financial export to cloud folder | 4 playbook reports → PDF/Excel → Google Drive via idempotent client; `drive.upload` job step; evidence packs and SuiteQL exports as downloads | Drive is the only cloud destination; BigQuery is read-only | **shipped-live (narrow)** | GCS/S3 implementation of the same `DriveClient` protocol |
| (d) Customization-aware scoping → PM tickets | Metadata discovery (custom fields/records/lists), content-hashed SuiteScript workspace tree, Celigo flow map, tenant memory graph | No scoping skill; **zero** PM integration | **absent (write half)** | Scoping skill (one grounded LLM call) + one PM connector with HITL preview and ticket idempotency |
| (e) Multi-threaded backfill | Cursor-based dedupe ingestion for Stripe/Shopify/Solidus/NetSuite deposits | Sequential; single-record NetSuite writes; no BigQuery load path; no durability | **prototype** | Chunked concurrency + bulk create with idempotency keys + per-job token budget; sell as Setup add-on |

First-customer demo anchor: **(b) as HITL detect-and-propose** plus (a)+(c) which are live. (e) is the paid add-on. (d) last.

### 3.5 Settings and Connections
- `settings/page.tsx` is a 2,921-line monolith with ~14 sections; NetSuite connectors live there while every other connector lives on `/connections`; Celigo renders on both.
- `PlanInfoSection` is pure plan/entitlement UI — delete.
- The "Company profile" the pivot wants (fiscal calendar, materiality thresholds, order-ref pattern) has **no read/write API**: the columns exist on `TenantConfig` but are absent from `TenantConfigResponse`/`Update`.
- The onboarding wizard duplicates Settings (`StepPolicy` ≈ `GovernancePolicySection`, `StepConnection` reimplements the NetSuite connect flow) — most of it moves into the installer.
- `PricingConfigSection` is reachable only from chat tool cards.

### 3.6 Operations gaps the hosting business depends on
- **No infrastructure-as-code, no TLS artifact, no frontend image pipeline, no migration runner** beyond two CI secrets; single static Fernet key with write-only `encryption_key_version`. All genuinely unbuilt.
- Framework's move to its own database is **L (1–2 weeks)**, not "reuse existing scripts": `export_tenant.py` covers 11 of ~46 tenant-scoped tables (reconciliation, transaction_ops, payouts/payout_lines with 400K+ rows, reports, schedules, workspace, memory, drive chunks with pgvector are all outside it). `reencrypt_tenant.py` is complete for today's three encrypted fields. Use `tenants.is_active=false` to quiesce Framework on staging (verify the four `*_all` sweeps honour it), then export → re-encrypt → import → copy the `workspace-data` volume (soul.md) → DNS. Reject pg_dump-and-delete: 44 of 65 tenant FKs lack `ON DELETE CASCADE` and it stages other tenants' data on Framework's box.
- Beat hygiene before any customer runs the stack: `audit_retention` is registered but **never scheduled** (tables grow forever); `auto_learning` has no enable flag and makes live NetSuite calls across tenants plus arbitrary web fetches; `knowledge_crawler` scrapes old.reddit.com with no robots.txt handling; `billing_sync` bills the vendor's Stripe; `agent_benchmark_vs_mcp` / `auto_query_improvement` are vendor-tenant-pinned; `example_sync` is dead.

## 4. Target architecture

### 4.1 Single-company mode (Option A)
```
install → scripts/install/bootstrap  →  register_tenant() [reused]
                                         plan='self_hosted', plan_expires_at=NULL
                                         seed_default_flags() with single-company defaults
                                         admin user + role   (fails loudly if roles table empty)
                                         seed_all_oracle_skills()
                                         prompt before seeding soul.md   (CLAUDE.md rule)
runtime → SINGLE_COMPANY=true            /register returns 404 once a tenant exists
                                         admin.py + /admin removed entirely
                                         PLAN_LIMITS collapsed to one permissive profile
                                         feature flags via PATCH /settings/features (tenant.manage)
```
`tenant_id`, RLS, `set_tenant_context`, invites, branding, soul config, workspace storage, Oracle-skill seeding: **unchanged**.

### 4.2 Deployment topology (per customer)
```
GCP project <customer>          Cloudflare DNS (free)
 └─ e2-standard-2 VM (COS)       <customer>.suitestudio.ai → VM
     docker compose:  caddy (HTTP-01 TLS) → frontend:3000, backend:8000
                      worker, beat, postgres16+pgvector, redis
     backups: WAL-G/pgBackRest → gs://<customer>-backups (daily base + WAL, 30d) + nightly disk snapshot
     secrets: Secret Manager → .env (mode 600) at boot
 fleet: OpenTofu root module per customer + `fleet` CLI (provision | upgrade | backup-verify | rollback)
 images: ghcr.io/suitestudio/{backend,frontend}@sha256:… pinned in fleet manifest
 upgrades: manifest bump → canary (staging + framework) → waves of 5; one-shot migrate container; expand/contract migrations only; freeze BD-2 … BD+7 each month
 observability: one Sentry org, project per customer; uptime checks every 5 min; per-project budget alerts
```
COGS model (per customer / month): LOW ≈ USD 75 · TYPICAL ≈ USD 195 · HIGH ≈ USD 775 (Cloud SQL HA + Opus-heavy). Sell HA DB, Opus reasoning, dedicated region, SSO as add-ons.

### 4.3 Installer
```
AGENTS.md  ──▶ docs/install/PLAYBOOK.md  (steps 0–10, each: goal · command · expected · verify · on-failure · idempotent?)
CLAUDE.md  first line: @AGENTS.md
.claude/settings.json (committed): allow Bash(make:*), Bash(./scripts/install/*); deny Read(./.env*), Bash(rm -rf *), Bash(docker system prune *), Bash(printenv*), …; ask Bash(make reset*)
.claude/hooks/install-guard.sh (PreToolUse, fail closed) · redact.sh (PostToolUse)
Makefile verbs: doctor · profile · env · secrets · up · migrate · verify · admin · connect-netsuite · smoke · status · logs · upgrade · backup · reset(CONFIRM=yes)
scripts/install/*.sh: idempotent, --json, one line per check, never echo a secret; .suitestudio/state.json ledger for resume
Profiles: local (bind-mount dev) · vm (Caddy TLS, systemd, ufw, backups) · managed (stop after `doctor --json`, hand to Suite Studio)
CI: tests/install/test_playbook.sh on a fresh Ubuntu container every PR; weekly `claude -p "install this repo"` smoke
```
Step 0 is profile choice and plan-then-execute. Steps that need a human: external secrets (typed into a TTY prompt or localhost form), the browser OAuth authorize for NetSuite, the NetSuite admin runbook (two integration records, role permissions, SDF deploy of the minimal SuiteApp, RESTlet URL + guard URL).

### 4.4 Settings / Connections information architecture (mock-first, per `report-design.md`)
```
/connections   NetSuite REST · NetSuite MCP · Stripe · Shopify · Solidus · BigQuery · Google Drive/Sheets · Celigo · Metabase · custom MCP · custom API
               uniform card: setup guide · Test · health · last sync · "configured by deployment" (read-only) where the installer set it
/settings      Company profile (name, fiscal year start, materiality thresholds, order-ref pattern, subsidiaries)  [needs new schema fields]
               AI (provider, model, keys, financial-report toggle, personality/soul)
               Team & roles (unchanged RBAC + invites)
               Notifications (email provider, digest)
               Branding (ungated)
               Scheduled system jobs (existing jobs section)
               Pricing (static home for PricingConfigSection)
               Deployment / About (version, build id, environment, license, update available)
removed        PlanInfoSection · /admin · custom-domain self-service · duplicate Celigo card · non-admin ConnectionStatusSection
onboarding     keep: NetSuite OAuth authorize, per-connector Test, first-success checklist; move to installer: profile, policy, workspace defaults
```

## 5. Milestones and sequencing

One senior engineer with coding agents. Weeks are elapsed, not effort. Selling (G-track) runs in parallel from week 1.

| Milestone | Weeks | Exit criterion |
|---|---|---|
| **M0 Decisions & prerequisites** | 1 | D1–D9 ratified; trademark search done; Framework consent for showcase; contribution-margin instrumentation live on Framework; credentials rotated |
| **M1 Single-company mode + Framework on its own stack** | 1–4 | `framework.suitestudio.ai` serves Framework from its own GCP project/VM/DB; staging tenant deactivated; day-15 outage fixed; admin/plan/billing surfaces gone; Beat hygiene done |
| **M2 Agent-walkthrough installer** | 3–6 | Three cold installs (Ubuntu + macOS, Claude Code + Codex) reach first SuiteQL with no secret in any transcript; CI cold-install test green |
| **M3 Public repository cut** | 5–8 | Fresh-history public repo under AGPL/Apache; README quickstart passes; external security review of auth/RLS/encryption closed; v0.1.0 + case study + partner page |
| **M4 Fleet v1** | 7–11 | `fleet provision <customer>` < 15 min; restore drill passes; canary→wave upgrade executed once; DPA + subprocessor list signed by Framework |
| **M5 Settings & Connections IA** | 9–13 | Approved HTML mock reproduced; connectors consolidated; Company profile editable; onboarding reduced |
| **M6 Selling points in revenue order** | 12+ | (b) completeness checks packaged with accounting-settings discovery; durable execution for write paths; (e) backfill engine as paid add-on; then (a)/(c) extensions; (d) last |

Public flip gate (end of M3): quickstart green on a clean clone, cold installs pass, security review closed, Framework consent, license files in, secrets scrubbed, ops repo split. The business gate the pre-mortem asks for (a second signed implementation) is tracked in G-01 and should not be *blocked* by the flip, but OSS work must never displace selling.

## 6. Go-to-market (parallel, human-led)
- Beachhead: Shopify/Stripe DTC brands on NetSuite, USD 20–200M revenue, 2–5 person accounting team, already paying Celigo/A2X/NetSuite Connector and still reconciling payouts in spreadsheets. Buyer controller/VP Finance; champion NetSuite admin; signer CFO.
- First 60 days: 10 discovery calls that never mention chat; ask each whether NetSuite Next / AI bank matching changes their interest; sell the second implementation (capped fixed fee with change-order clause; acceptance on one specific closed month with an exclusion list).
- Assets: Framework case study, partner page (NetSuite consultancies co-sell for revenue share / trademark license), SDN free tier now, Built for NetSuite later, suitestudio.ai marketing site once positioning is validated.
- Capacity: contract a part-time NetSuite implementer and a part-time SRE before customer four; close-week on-call rota; decline 24/7 SLA deals until a second person exists.
- What must be true (test, do not assume): second company pays USD 15–35K on the current code within 60 days; setup for customer three < 80 founder hours; stack fits `e2-standard-2` at Framework volume; LLM < USD 150/month typical; contribution margin > 60% by month three; a non-founder completes the playbook cold; five prospects' security questionnaires do not all require SOC 2 up front.

## 7. Risks and mitigations (top eight)
| Risk | Likelihood | Mitigation in this plan |
|---|---|---|
| Services trap: founder becomes the delivery department | high | Hour logging from day one; every step done twice becomes a script; contractor implementer before customer four |
| Oracle bundles the chat/close wedge | high | D3 positioning; build on AI Connector; headline cross-system + completeness + backfill |
| Open source yields no customers | high | Treat as trust asset; measure inbound quarterly; time-box community hours |
| Second customer reveals a Framework-shaped product | high | Accounting-settings discovery step first; capped fixed fee; acceptance on one closed month |
| Refactor eats two quarters | high | Option A (topology, not refactor); no `tenant_id` removal |
| Agent install fails or leaks a secret | medium | Allow-listed scripts, deny rules, fail-closed hooks, CI cold install, managed profile as default for non-technical buyers |
| USD 999 is a subsidy | medium | Envelope + Hosted Plus + per-job token budgets + BYOK; contribution-margin metric |
| Synchronized close week + one bad fleet upgrade | medium | Code-enforced deploy freeze BD-2…BD+7; canary→waves; nightly restore-verify |

## 8. Ticket map
Tickets live in ClickUp list "Pivot · Open-Source Suite Studio" with one parent task per milestone (M0–M6 + GTM). Each ticket names the files, the acceptance criterion, and its tier per `CLAUDE.md` (single-company mode, installer secrets handling, fleet, and anything touching auth/RLS/credentials are **T2**).

Existing tickets to close as superseded: 86ba3bgpq, 86ba3bgzf, 86ba3bh55, 86ba3bh6g, 86ba3bh4b, 86ba3bgy2 (desktop B0–B4), 86babkn74 (Bet 1 desktop), 86ba3bh72 / 86ba3bh81 / 86ba3bh92 (desktop OQs).

## 9. Open questions carried into tickets
1. Oracle terms: may a third party operate a hosted multi-customer service calling the AI Connector on customers' behalf; SDN rules for a "for NetSuite" open-source product; whether "Suite Studio" clears the Suite* family. (Oracle pages were proxy-blocked; verify from an unblocked network.)
2. Customer-side NetSuite bill of materials: integration user license, SuiteCloud Plus for concurrency if backfills are real, sandbox account for HITL testing, service tier governing the shared concurrency limit.
3. Compliance paperwork before a second company's finance data is hosted: DPA + subprocessors (Anthropic, Google Cloud, Sentry, Cloudflare), PCI scope of mirrored Stripe data, EU inference via Vertex regional endpoints.
4. Channels for the first five customers: NetSuite Professionals Slack, r/Netsuite, SuiteWorld, Shopify Plus / Stripe agencies, CFO firms; and Stripe's own NetSuite connector as the direct substitute.
5. Does the full stack fit `e2-standard-2` at Framework volume (400K+ payout lines)? Measure before promising RPO/RTO.
6. Measured Framework token volume (Anthropic Usage API) — the USD 40/110/400 LLM figures are modeled.

---

## Appendix A — Sources the plan leans on
Verified live during research (Sept 2026): Anthropic pricing (Sonnet 5 USD 2/10, Opus 5 USD 5/25, Haiku 4.5 USD 1/5 per MTok; batch 50% off); GCP Compute Engine, GKE, Cloud SQL pricing pages; Medplum pricing (USD 2,000 / 6,000); Claude Code memory/hooks/permissions docs; GitHub API star counts for "netsuite mcp"; OpenClaw onboard/doctor docs; Coolify/Twenty/Plane installers; Sentry FSL text; Metabase/Twenty/Windmill/Plausible license files; PostHog self-host retirement post.
Reached only via secondary sources (re-verify before quoting in a deck): NetSuite Next / AI Connector timelines and terms, NetSuite managed-services and partner rate bands, Celigo/A2X/Synder/FloQast/Kolleno price points, Frappe Cloud tiers, SOC 2 cost bands.

## Appendix B — Effort key
S < 1 day · M 1–3 days · L 1–2 weeks · XL > 2 weeks, one senior engineer with a coding agent.
