# Pivot tickets — ClickUp mirror and pending items

> Companion to `2026-09-13-open-source-single-company-pivot.md`.
> ClickUp list: **Pivot · Open-Source Suite Studio** — https://app.clickup.com/90141183599/v/l/li/901421046222 (AI Research › AI-den).
> Status 2026-09-13: 8 parents + 24 subtasks created; **15 subtasks (S-03…S-06, V-01…V-06, G-01…G-05) and the dependency links are pending** because the ClickUp connector's daily rate limit was hit. They are reproduced in full below so nothing is lost; a scheduled follow-up creates them when the quota resets.

## Created

| Key | ClickUp | Title |
|---|---|---|
| M0 | 86bbzv997 | Decisions & prerequisites (week 1) |
| P-01 | 86bbzva8b | Decide license + trademark |
| P-02 | 86bbzva97 | Ratify positioning, pricing envelope, design-partner terms |
| P-03 | 86bbzva9g | Framework consent: showcase, case study, fixture policy |
| P-04 | 86bbzva9q | Instrument contribution margin per deployment |
| P-05 | 86bbzva9x | Rotate credentials + baseline secret scan |
| M1 | 86bbzv99e | Single-company mode + Framework on its own stack (weeks 1–4) |
| T-01 | 86bbzvabg | SINGLE_COMPANY bootstrap CLI |
| T-02 | 86bbzvabr | Fix day-15 outage; collapse PLAN_LIMITS |
| T-03 | 86bbzvacj | Single-company feature-flag defaults + toggle surface |
| T-04 | 86bbzvadg | Delete platform admin / impersonation; gate /register |
| T-05 | 86bbzvadt | Remove TenantWallet / billing / custom-domain self-service |
| T-06 | 86bbzvae3 | Beat hygiene |
| T-07 | 86bbzvaee | Framework migration tooling |
| T-08 | 86bbzvaf1 | Stand up framework.suitestudio.ai and cut over |
| T-09 | 86bbzvafm | Config hygiene (os.environ → Settings, Google client id, GoogleLogin, Redis) |
| T-10 | 86bbzvafy | MultiFernet key rotation |
| M2 | 86bbzv99j | Agent-walkthrough installer (weeks 3–6) |
| I-01 | 86bbzvah7 | Make `docker compose up` work from a clean clone |
| I-02 | 86bbzvavn | scripts/install verbs + resume ledger |
| I-03 | 86bbzvb2g | AGENTS.md + CLAUDE.md shim + PLAYBOOK/PROFILES/TROUBLESHOOTING + skill |
| I-04 | 86bbzvba7 | Guardrails in code (settings.json, hooks, redaction) |
| I-05 | 86bbzvbdv | NetSuite admin runbook + minimal SuiteApp fixes |
| I-06 | 86bbzvbeb | Prove the installer (CI cold install, agent smoke, manual cold installs, telemetry) |
| M3 | 86bbzv99p | Public repository cut (weeks 5–8) |
| O-01 | 86bbzvbex | Scrub internal identifiers; split private ops repo |
| O-02 | 86bbzvbfc | Delete dead subtrees and dead code |
| O-03 | 86bbzvbfx | Customer-agnostic code and fixtures |
| O-04 | 86bbzvbg6 | Coordinated rename to Suite Studio |
| O-05 | 86bbzvbgb | LICENSE files, NOTICE, license scanner |
| O-06 | 86bbzvbh0 | README rewrite + CONTRIBUTING/SECURITY/CoC/"always open" |
| O-07 | 86bbzvbh5 | Public CI + public images |
| O-08 | 86bbzvbhb | Pre-publish security pass + fresh history |
| O-09 | 86bbzvbhe | Flip public and launch v0.1.0 |
| M4 | 86bbzv9ah | Fleet v1 — per-customer hosting (weeks 7–11) |
| F-01 | 86bbzvbj0 | OpenTofu module per customer + fleet CLI |
| F-02 | 86bbzvbj9 | Frontend image strategy for per-customer hosts |
| F-03 | 86bbzvbjg | Fleet upgrades: canary → waves, migrate container, deploy freeze |
| F-04 | 86bbzvbju | Backups and DR |
| F-05 | 86bbzvbjz | Fleet observability |
| F-06 | 86bbzvbkn | Compliance track (DPA, SSO, SOC 2 plan, PCI, EU inference) |
| F-07 | 86bbzvbkt | Hosted Plus add-ons |
| M5 | 86bbzv9d8 | Settings & Connections IA for one company (weeks 9–13) |
| S-01 | 86bbzvbkx | HTML mock for Connections hub and Settings |
| S-02 | 86bbzvbma | Company profile backend (fiscal year, thresholds, order-ref pattern) |
| M6 | 86bbzv9fn | Selling points in revenue order (weeks 12+) |
| GTM | 86bbzv9jt | Sell the second implementation (parallel from week 1) |

## Pending — create under M5 (86bbzv9d8)

### S-03 Consolidate every connector onto /connections with one card contract · priority normal
Today NetSuite lives in Settings (`NetSuiteConnectionSection` inline at `settings/page.tsx:1571` **and** `components/settings/netsuite-connections-section.tsx` rendered at `:2889` — check whether both render), every other connector lives on `/connections`, and `CeligoConnectorCard` renders on both pages behind the same flag.

Move to `/connections` (UI only; backend routes are already RBAC-gated and fine): `netsuite-connections-section.tsx` (928 lines), `NetSuiteMetadataSection` (`page.tsx:1826-2198`) and `SuiteScriptFilesSection` (`:2201-2355`) as sub-panels of the NetSuite card, `bigquery-connection-section.tsx` (836 lines), `data-source-connectors-section.tsx` (+ `stripe-connector-card.tsx`, `sheets-connector-card.tsx`, `drive-folders-section.tsx`, `netsuite-deposit-sync-card.tsx`), `celigo-connector-card.tsx` (delete the Settings instance), Metabase (already there), `AddConnectionDialog` / `AddMcpConnectorDialog` (the latter is live, not orphaned).

Card contract: label, provider, status/health, last sync, detail line, setup-guide link (docs/install), Test, Reconnect/Authorize where OAuth, Delete, and a read-only "configured by deployment" block. Generic "Solidus" wording (O-03). Reproduce the approved mock (S-01); vitest for the card contract; `npx tsc --noEmit`; eslint. Blocked by S-01.

### S-04 Split the 2,921-line settings page; delete PlanInfoSection; ungate Branding; add Deployment/About and a static Pricing entry · priority normal
Delete `PlanInfoSection` (`:163-243`, `usePlanInfo`, `PLAN_TIERS`, `GET /tenants/me/plan` — T-02 removes the backend). Extract and move: `AiConfigSection` (`:560-789`) + `ChatSettingsSection` (`:792-884`) → AI tab; `SoulSection` (`:1112-1297`) → AI (respect "soul config is sacred"); `TenantProfileSection` (`:910-1109`) → AI as "business context"; `BrandingSection` (`:249-386`) → Branding, drop the `custom_branding` gate, remove the custom-domain card (T-05); `GovernancePolicySection` (`:2379-2658`) → shared `PolicyForm` also used by onboarding (S-05); `ConnectionStatusSection` (`:2736-2775`) → delete; `jobs-section.tsx` → Scheduled system jobs; `team-section.tsx` → Team & roles (unchanged). New `DeploymentAboutCard`: version, build id (`/version`), environment, license, update-available. `PricingConfigSection` (today only inside chat tool cards, `message-list.tsx:842,946`) gets a static home under Pricing. Keep `useFeature` for the genuine operator toggles (T-03 table). Reproduce the mock; page-level vitest. Blocked by S-01, T-02.

### S-05 Reduce the onboarding wizard; dedupe StepPolicy vs GovernancePolicySection · priority normal
`components/onboarding/steps/*` duplicates Settings: `step-policy.tsx` is a near copy of `GovernancePolicySection` (same `TOOL_OPTIONS`, same `POST /api/v1/onboarding/setup-policy`); `step-connection.tsx` reimplements the NetSuite OAuth/MCP connect flow (`openOAuthPopup` at `page.tsx:1642-1826`). Extract one `PolicyForm`; `StepConnection` reuses the connections card's connect logic (stays — OAuth needs a browser); `StepProfile`, `StepPolicy`, `StepWorkspace` become installer questions (`make admin` → `POST /onboarding/profiles`, `/setup-policy`, `POST /workspaces`) with defaults; `StepFirstSuccess` becomes an in-app checklist; onboarding copilot uses the generic avatar (O-03). Blocked by S-01, I-02.

### S-06 "Configured by deployment" read-only pattern + tenant→company wording (T0) · priority low
Pick one pattern for installer-set values (disabled inputs inside the connector card with a "set by deployment — re-run `make secrets`" hint, recommended). Backend `GET /api/v1/settings/deployment` reporting which config keys are env-sourced (names + set/unset, never values). Wording "tenant" → "company"/"organization" in UI labels and API docs; do not rename tables or models. Blocked by S-01.

## Pending — create under M6 (86bbzv9fn)

### V-01 (b) Package "ERP completeness checks": accounting-settings discovery, findings catalogue, HITL proposal flow, structured learned-rule schema · priority high · T2
The strongest half exists: `services/transaction_ops/*` detects missing orders, amount and VAT differences, re-reads native subledger detail (`accounting_evidence.py:29-77`), enforces period-open and balanced-entry invariants (`posting_invariants.py`), produces immutable proposals a human approves, executes with a live re-read guard. Missing: it is Framework-shaped; "learned rules" are free text injected into the prompt (`tenant_learned_rule` has no link to any executor); no autonomy (V-02, V-06).

Do: accounting-settings discovery as the first step of every engagement and of the installer's post-connect phase (subsidiaries, currencies, tax engine, fiscal calendar, open periods, custom records/fields, order-ref pattern) stored on `TenantConfig`/`transaction_ops_configs` and shown in Company profile (S-02); a findings catalogue (missing order, amount variance, VAT variance, duplicate Celigo error, unapplied deposit, wrong period …) with evidence needed and proposal template; `framework_defaults.py` → per-deployment mapping (O-03); structured learned rules `finding_type → proposal template → approval-count threshold`, cards pre-filled and marked "matches rule R (approved 12×)", **still human-approved** until V-06; `case_service.py` consults rules before surfacing a card. Sell as "evidence-grounded proposals a human approves".

### V-02 Durable execution for every NetSuite write path · priority high · T2
Confirmed: no `acks_late` anywhere; `--concurrency=2` fixed; only 10 of ~34 task modules set a time limit; every `transaction_ops` task hardcodes `max_retries=0`. Build the posting-ladder P3 primitives (`docs/superpowers/plans/2026-08-02-autonomous-accounting-ops-program.md`): `netsuite_posting_log` side-effect log with work-derived idempotency key, `started → posted | failed`, kind/tries/`first_failed_at`/`trace_id`, written **before** every external write; stale-`started` sweeper; external-ref stamping; Celery `acks_late` + `reject_on_worker_lost` + `autoretry_for` with backoff + hard time limits; replayable DLQ; compensation registered before execute; irreversible steps last. Required gate: staging `kill -9` mid-write drill (`scripts/uat/transaction_ops_crash_drill.py`) shows exactly one write and clean recovery. Blocks V-03, V-06.

### V-03 (e) Backfill engine · priority normal · T2
Sequential cursor-based upsert today; single-record NetSuite writes; no fan-out; BigQuery read-only. Do: chunk by date/id range; Celery chord fan-out with a bounded `Semaphore` respecting the per-account concurrency limit and 60-second/24-hour frequency windows; backoff on 429 / `SSS_REQUEST_LIMIT_EXCEEDED`; bulk create with per-item idempotency keys and side-effect log (V-02), resumable; per-job token and API-call budgets enforced at spend granularity (a 400K-line backfill is USD 1–5K of tokens at list price — the job stops at the cap); Batch API + Haiku for mechanical steps; BigQuery load path behind an explicit write flag and the HITL guard. Sold as a Setup add-on per million lines with a token cap. Blocked by V-02.

### V-04 (a)+(c) Scheduled-workflow and export extensions · priority normal · T1/T2
Registry (`services/jobs/registry.py:580-624`) has six types; export is Drive-only across four playbooks. Add `gcs.upload` / `s3.upload` (same `DriveClient`-shaped protocol), `email.deliver`, `sheets.write`; more playbooks (AR/AP aging, cash position, Stripe payout summary, reconciliation exception digest) mock-first per `executive-dashboard-design`; a public Apache-2.0 `templates/` gallery the compiler is seeded from. NetSuite/Celigo writes stay out of the registry. Verify the chat hand-off card from Slice 2 Task 7 shipped.

### V-05 (d) Scoping skill + first PM connector (ClickUp) with HITL preview and ticket idempotency · priority low · T2
Raw material exists (metadata discovery, SuiteScript workspace tree, Celigo flow map, memory graph); no scoping skill; **no PM integration anywhere**. Build last, after a prospect names it. `services/scoping/`: one grounded LLM call → scope-of-work report (affected customizations, risks, steps, estimate bands, open questions), mock first. First PM connector ClickUp via the generic MCP connector path; prerequisite: ClickUp 86baf52mu (default-deny unknown external write tools). HITL preview card mirroring `write_confirmation_service.py`; work-derived ticket idempotency key. Jira/Linear later.

### V-06 Autonomy Rung 1 · priority low · T2
Ladder per `docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md`. The reject action shipped (PR #193/#196; STATE.md is stale). Confirm reject is exposed in UI + chat tool so labels accrue; dashboard the false-positive rate; flip `recon_envelope_dry_run` to real auto-approval of **DB status only** for `matches` / `deterministic` / zero variance / run not closed, behind `autonomous_recon` (default off) with a kill switch and cost budget, `actor_type="system"` audit; publish the envelope and measured error rate; Rungs 2/3 remain separate decisions gated on labels and V-02. Learned-rule fast-track (V-01) may auto-approve only inside this envelope. Blocked by V-02.

## Pending — create under GTM (86bbzv9jt)

### G-01 Sell the second implementation · priority urgent
"What must be true" #1: a second NetSuite company with different subsidiaries/currencies/tax engine pays USD 15–35K for a Stripe/Shopify-to-NetSuite reconciliation implementation on the **current** code within 60 days. Target list of 30 Shopify/Stripe DTC brands on NetSuite (USD 20–200M revenue, 2–5 person accounting team, on Celigo/A2X/NetSuite Connector) from the NetSuite Professionals Slack, r/Netsuite, Shopify Plus agencies, CFO/accounting firms, Framework's network. 10 discovery calls that never mention chat: close length, payout reconciliation hours, connector failure points (payout reconciliation, FX rounding, Avalara vs NetSuite tax, partial refunds), what they pay today, and whether NetSuite Next / AI bank matching changes their interest; record which selling points anyone would pay for. Offer: capped fixed fee with change-order clause; acceptance on one specific closed month with an exclusion list; 30 days hypercare; design-partner terms (P-02). Collect security questionnaires before quoting (F-06). Log founder hours (target < 80 h by customer three). Owner: Aiden.

### G-02 Framework case study, reference calls, design-partner agreement · priority high
Written reconciliation case study (close time before/after, payout lines per month, exception categories, the July 2026 statement composed unattended, the transaction-ops evidence flow) with the controller's quotes; design-partner agreement per P-02 (50% off Hosted for 12 months, price-locked 24, two reference calls, monthly feedback, logo use); consent for the fixture policy (P-03). Blocked by P-03. Feeds O-09, G-03.

### G-03 suitestudio.ai marketing site · priority normal
After G-01 validates the message and O-09 exists to link to. Headline on what Oracle will not bundle; "for NetSuite" tagline, never "NetSuite" in the name. Two paths: "Run it yourself — open the repo in Claude Code or Codex and say *install this*" and "Have us run it — Setup package + Hosted from USD 999/month" with the envelope and Hosted Plus. Pricing page (P-02), case study (G-02), partner page (G-04), security page (F-06). Replaces `landing-page/` (deleted in O-02; its "Multi-Tenant Security" card is now wrong). Mock first. Blocked by G-01, O-09.

### G-04 Partner page and NetSuite ecosystem presence · priority normal
Join SDN at the free tier now; Built for NetSuite later. Partner program one-pager: consultancies may deploy and support Suite Studio for clients; use of the name/logo requires the partner agreement (revenue share on Hosted referrals or upstream contribution); modified builds must be renamed (TRADEMARK.md). List two or three friendly consultancies to co-sell and as the contractor pool (G-05). Verify Oracle terms for a hosted third party calling the AI Connector (P-01) first. Blocked by P-01.

### G-05 Capacity and bus factor · priority high
Written weekly split (e.g. 2 days delivery/support, 3 days product); hours logged per customer (P-04); every setup step done twice becomes a playbook script; before customer four a part-time NetSuite implementation contractor (USD 125–175/hour) and a part-time SRE or partner firm in the on-call rota; close-week on-call as a paid add-on; no 24/7 SLA deals until a second person exists; customer-executable "Suite Studio is unavailable" runbook (F-04); monthly interrupt/hours audit; cap hosted customers at three until the rota exists.

## Amendments to already-created tickets (apply as a comment or description edit when quota allows)

Source: plan §3.7 (Oracle-side constraints), added after these tickets were created.

- **P-01 (86bbzva8b)** — add: always write the name as two words "Suite Studio AI", never CamelCase; commission a professional USPTO/EUIPO clearance search in classes 9 and 42 and file before publication; pre-register a fallback name + domain; add an Oracle credit line and a "not affiliated with, endorsed by, or certified by Oracle or NetSuite" notice to every page; never use the phrase "Built for NetSuite" without certification; SDN membership is not required for GitHub distribution (SSA Section 1 sharing right; Oracle's own UPL-1.0 samples); position as extending NetSuite to stay clear of SSA 3.1.2(b); one company per deployment with no shared credentials satisfies 3.1.2(c); a human captures the verbatim AI Connector acknowledgment text before launch.
- **I-05 (86bbzvbdv)** — add the customer bill of materials: one licensed Employee with a custom non-Administrator role (REST Web Services + "Log in using OAuth 2.0 Access Tokens" + "MCP Server Connection"; the "Web Services Only" flag blocks RESTlets); per-customer integration records (Authorization Code + PKCE, consent "Always Ask", redirect at the customer's subdomain, never a shared client id); sandbox tokens are destroyed on refresh → the runbook needs a re-onboarding step; SuiteQL 100,000-row cap; HIPAA/BAA accounts cannot activate the AI Connector; run `doctor` to detect the account's service tier and print its concurrency ceiling.
- **O-07 (86bbzvbh5)** — add: do not redistribute the SuiteCloud SDK jar inside public images (Oracle Free Use Terms: unmodified and fee-free only); install `@oracle/suitecloud-cli` at container start / first use, or build the workspace-deploy image privately.
- **V-03 (pending)** — the concurrency design is bounded by the customer's tier: Standard 5 (max +1 SuiteCloud Plus → 15), Premium 15 (→45), Enterprise 20 (→80), Ultimate 20 (→140); 429 `CONCURRENCY_LIMIT_EXCEEDED` on the shared pool; quote backfills per account tier and never promise lanes the tier cannot provide.
- **F-06 (86bbzvbkn)** — add HIPAA/ePHI exclusion to customer terms (AI Connector "not assessed for HIPAA"); replace the body with the compliance pack in plan §3.8: Art. 28 DPA + SCC Module 3 + UK Addendum + Annexes; public sub-processor register; Anthropic per-org zero data retention via sales with the Covered-Models (Fable/Mythos 30-day retention) disclosure and Opus 5 / Sonnet 5 as the hosted default; EU residency via Vertex `eu` multi-region (+10% tokens; no Files API/Batches on Vertex); PCI no-CHD scoping statement + PAN regex scanner on ingestion, transcripts and logs (product change); CCPA §7051(a) clauses; `EXPORT.md` + embargoed-region geo-block; **EU AI Act Article 50 "you are chatting with an AI system" disclosure in the chat UI and job emails (in force since 2 Aug 2026 — product change, S, do first)**; SOC 2 first-year budget USD 15–35K (Secureframe/Sprinto/Vanta Essentials + boutique auditor), Type 1 in 6–12 weeks.
- **G-01 / G-04 (pending)** — incorporate the channel plan from plan §6: CPA / fractional-CFO NetSuite practices as the primary referral track (SuiteAccountants enrolment, 10–15% referral, firm keeps post-go-live support), the Stripe-gap positioning (Shopify Payments / PayPal / Amazon / multi-processor payouts that Stripe's own connector will not reconcile), `#ai-netsuite` Slack + r/Netsuite answers as SEO, SuiteWorld 2026 (Oct 25–28) as attendee with partner happy hours and a pre-event dinner, day-60 kill criterion.

## Dependency links to add (waiting_on)

Critical path first (create these when quota allows), then the rest.

| Task | waits on |
|---|---|
| T-08 | T-07, T-01, T-02, T-03, T-09 |
| I-02 | I-01, T-01, T-09 |
| I-03 | I-02 |
| I-06 | I-01, I-02, I-03, I-04, I-05 |
| O-03 | P-03 |
| O-04 | O-01, O-02 |
| O-05 | P-01 |
| O-06 | I-01, O-05 |
| O-08 | O-01, O-03, I-04, T-10 |
| O-09 | O-01, O-02, O-03, O-04, O-05, O-06, O-07, O-08, I-06 |
| F-02 | O-07 |
| F-07 | F-01, F-06 |
| S-03, S-04, S-05, S-06 | S-01 (S-04 also T-02; S-05 also I-02) |
| V-03, V-06 | V-02 |
| G-02 | P-03 |
| G-03 | G-01, O-09 |
| G-04 | P-01 |

## Existing tickets to close as superseded by the pivot (operator action)
86ba3bgpq, 86ba3bgzf, 86ba3bh55, 86ba3bh6g, 86ba3bh4b, 86ba3bgy2 (desktop B0–B4), 86babkn74 (Bet 1 desktop cadence), 86ba3bh72, 86ba3bh81, 86ba3bh92 (desktop open questions). Also review 86baf52mu (MCP HITL generalization) — it becomes a prerequisite of V-05 rather than a standalone item.
