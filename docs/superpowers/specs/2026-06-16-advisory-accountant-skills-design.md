# Advisory Accountant + Financial-Analytics Skills — Design Spec

> **Status:** approved design (brainstorm complete) → next: writing-plans
> **Date:** 2026-06-16
> **Branch:** `feat/advisory-accountant-skills`
> **Program:** Phase 1 of the self-improving memory & skills program (② skills track).
> ① Tenant Memory Graph shipped (PR #134). This is the planned-next, read-only/advisory.
> **Tier:** T2 (knowledge-profile / chat-prompt = prompt-pollution surface).

## 1. Goal

Give the unified chat agent a senior-accountant / FP&A toolkit: authoritative,
step-by-step **methodology playbooks** for the highest-value controller workflows, plus an
always-on **GAAP-grade interpretation lens** over financial reports and named metrics.

These are **read-only / advisory**. They diagnose, analyze, and *recommend* — they never
post, write, or mutate accounting data. Posting stays in the separate, operator-locked,
always-HITL P3 track.

## 2. Scope

**In scope**
- 7 new runtime `SKILL.md` files in `backend/app/services/chat/skills/<slug>/SKILL.md`,
  using the existing skill mechanism (slash command + semantic trigger, progressive
  disclosure of an authoritative body).
- 1 new knowledge profile `backend/app/services/chat/knowledge_profiles/financial_analysis.yaml`
  triggered on `netsuite_financial_report` / `metric_compute`.
- TDD test coverage (registry, trigger hygiene/routing, no-pollution, no-LLM-numbers,
  profile activation).

**Out of scope (explicit)**
- **No posting / no writes.** Nothing here calls a mutation tool. Recommendations are prose.
- **No per-tenant skill *learning*.** These are global static files like the existing 4
  skills. Per-tenant skill personalization is P2 (③).
- **No new tools.** Skills compose existing tools only.
- **No QuickBooks-specific skill.** QuickBooks has zero integration today (no connector, no
  tools). The bookkeeping skill is *source-agnostic* and lights up for QBO automatically if
  and when a connector + tools land. (See §5.7.)
- **No RAG corpus** for the profile yet (`rag_partitions: []`).
- **No frontend change.** The slash-command menu is already data-driven (see §7).
- No Budget-vs-Actual skill (deferred; depends on budget-record availability).

## 3. Background: how the machinery works (verified)

**Skills** (`backend/app/services/chat/skills/__init__.py`)
- Each `SKILL.md` has frontmatter `Name`, `Description`, `Triggers` (a list of slash commands
  and/or semantic phrases). Body = authoritative instructions.
- `match_skill(user_input)` runs on **every** turn in `UnifiedAgent._setup_context`. It
  matches (1) the first word if it's a slash command, else (2) the first skill whose semantic
  trigger phrase is a **substring** of the message. **Only one skill matches per turn; first
  hit wins in `sorted(slug)` order.**
- A matched skill's body is injected as `<skill_instructions>` with "follow step-by-step, do
  NOT deviate". Unmatched turns get a lean `<available_skills>` list (slash + name + desc).
- `GET /skills/catalog` returns `get_all_skills_metadata()` (name, description, triggers, slug)
  — consumed by the frontend menu.

**Knowledge profiles** (`knowledge_profiles/loader.py`)
- YAML: `profile_id`, `display_name`, `trigger_tools` (exact names or fnmatch globs),
  `prompt_fragment`, `rag_partitions`.
- A profile activates when any `trigger_tools` entry is present in the turn's active tool
  list (`matches_tools`), injecting `prompt_fragment`. Matching is against the **sanitized**
  tool names the LLM sees (e.g. `bigquery_sql`, `netsuite_financial_report`,
  `metric_compute`) — confirmed: `bigquery.yaml` uses `bigquery_sql`, not `bigquery.sql`.

**The departure from the existing 4 skills:** the current skills (`period_comparison`,
`sales_by_platform`, `inventory_check`) embed hardcoded SuiteQL with tenant-specific columns
(e.g. `custitem_fw_platform`, `transactionline`). That is the prompt-pollution we must avoid.
The new skills carry **no SQL and no tenant columns** — they are methodology + GAAP reasoning
+ standard thresholds, delegating every number to tools.

## 4. Design spine (non-negotiables)

1. **Methodology playbooks, not SQL macros.** Zero embedded SuiteQL / tenant columns. Skills
   prescribe *what an accountant does and why*, and name the **tool** to fetch each figure
   (`netsuite_financial_report`, `metric_compute`, schema-discovered `netsuite_suiteql`).
   Naming a tool is allowed; naming a tenant column is pollution.
2. **No-LLM-numbers, enforced structurally.** Tool results are intercepted into SSE
   `financial_report` / `data_table` and condensed for the LLM. Every skill body ends with a
   standardized output-discipline directive: *the tool renders the figures automatically —
   narrate insight, never restate or recompute numbers.* Multi-section narratives use
   `report_compose` with `result_id` references (per `reporting.yaml`).
3. **Global static files, memory-aware for free.** No learning loop. The shipped tenant-memory
   read-loop already injects confirmed concepts + learned rules into the agent's system
   prompt, so these skills automatically benefit from per-tenant memory with no extra wiring.
4. **Always UnifiedAgent.** No persona, no sub-agent, no routing. Skills/profile only augment
   the single agent's prompt.

## 5. The 7 skills

Each skill body follows the same shape: **(1) scope/inputs → (2) fetch via the named tool(s),
discovering schema where needed → (3) apply the standard method + thresholds → (4) GAAP-aware
narrative → (5) output discipline (do not restate numbers).** Thresholds below are universal
finance/GAAP conventions (not tenant schema), so they are allowed in the prompt.

### 5.1 P&L Flux / Variance — `pl_flux_variance` — `/flux`
- **Method:** compare two income-statement periods (MoM / QoQ / YoY / vs-budget). Compute
  line-level $ and % deltas. Flag any line whose movement exceeds **materiality** (default:
  the greater of a user-stated threshold or **5% of the prior-period line**; state the
  threshold used). Attribute each material variance to a driver class: **volume / price / mix
  / timing-or-accrual / one-off**.
- **Tools:** `netsuite_financial_report` (income statement, two periods) → optional
  `report_compose` for a narrated flux report.
- **Output:** ranked table of material variances (tool-rendered) + driver narrative. No raw
  numbers in prose.

### 5.2 AR / AP Aging Triage — `ar_ap_aging_triage` — `/aging`
- **Method:** branch on **AR** vs **AP** (ask if ambiguous). Pull the open-item aging
  schedule, bucket **0-30 / 31-60 / 61-90 / 90+** days. Compute **DSO** (AR) or **DPO** (AP).
  Surface concentration (top counterparties by overdue balance) and a **prioritized action
  list** — collections sequence for AR; payment-timing / early-pay-discount capture for AP.
- **Tools:** `netsuite_financial_report` (aging report if available) or schema-discovered
  `netsuite_suiteql` over open transactions (discover columns first; no hardcoded schema).
- **Output:** bucketed aging table + DSO/DPO (tool-rendered) + prioritized prose list.

### 5.3 Ratio Analysis — `ratio_analysis` — `/ratios`
- **Method:** compute the standard ratio panel from Balance Sheet + Income Statement:
  - **Liquidity:** current ratio, quick ratio
  - **Leverage:** debt-to-equity, interest coverage
  - **Profitability:** gross / operating / net margin, ROA, ROE
  - **Efficiency:** asset turnover, inventory turnover, receivables turnover
  Benchmark each against the standard convention range **and** the prior period; narrate
  financial health and any deterioration.
- **Tools:** `netsuite_financial_report` (BS + IS); `metric_compute` for any blessed ratio
  that already exists in the metric catalog (prefer the blessed metric over recomputation).
- **Output:** ratio panel table (tool-rendered) + health narrative.

### 5.4 Month-End Close Checklist — `month_end_close` — `/close-checklist`
- **Type:** **meta, by reference.** Skills cannot invoke each other (one match per turn), so
  this walks the controller through the standard close sequence and *references the same
  methodology* used by the other skills inline as steps:
  1. Subledger reconciliation (incl. the product's recon engine for Stripe/deposits)
  2. **Books review / GL hygiene** (see §5.7) — clear suspense/clearing, fix coding
  3. Accruals, prepaids, deferrals, depreciation; cutoff review
  4. Intercompany / elimination check (if applicable)
  5. **Flux review** of the P&L (§5.1 method)
  6. Balance-sheet account reconciliation
  7. **Ratio sanity check** (§5.3 method)
  8. Lock / period-close readiness summary — *advisory; never performs the lock.*
- **Tools:** none directly; it orchestrates the agent through the other playbooks + existing
  read tools.
- **Output:** a checklist with status per step + a readiness summary. No posting, no lock.

### 5.5 Gross-Margin Bridge — `gross_margin_bridge` — `/margin-bridge`
- **Method:** decompose the change in gross margin between two periods into **price /
  volume / mix / cost** effects (PVM bridge). Requires line-level revenue, quantity, and unit
  cost — **discover the schema first** (do not assume column names), then build the bridge.
- **Tools:** schema-discovered `netsuite_suiteql` (line-level) + `netsuite_financial_report`
  for the margin totals to reconcile the bridge against.
- **Output:** waterfall-style bridge table (tool-rendered) + driver narrative. If line-level
  data is unavailable, say so and fall back to a top-level margin variance (no fabrication).

### 5.6 Cash-Flow / Runway — `cash_flow_runway` — `/cashflow`
- **Method:** narrate the statement of cash flows (operating / investing / financing), then
  derive **runway** from the cash-balance trend and average net monthly burn. Call out
  liquidity risk.
- **Tools:** `netsuite_financial_report` (cash-flow statement + cash balance trend).
- **Output:** cash-flow summary table + runway estimate (tool-rendered figures) + narrative.

### 5.7 Books Review / GL Hygiene — `books_review` — `/books-review`
- **Source-agnostic** bookkeeping-review playbook (the in-scope answer to the QuickBooks +
  NetSuite bookkeeping ask). Read-only.
- **Method:** inspect the general ledger for hygiene issues:
  - Uncategorized / suspense / clearing-account balances that should be zero
  - Unreconciled bank / control accounts
  - Duplicate or likely-missing entries; out-of-balance subledgers
  - Account-coding inconsistencies; postings to inactive / disallowed accounts
  - Accrual / cutoff gaps; negative balances where impossible (e.g. negative inventory cost)
  Produce a **recommended adjusting-entry / cleanup list** — *advisory only; never posts.*
  That list is the natural on-ramp to the P3 HITL-posting approval queue.
- **Platform-agnostic:** methodology, not platform SQL. Runs on NetSuite today via existing
  read tools; auto-extends to QuickBooks the moment a QBO connector + tools exist (the skill
  reads "whatever GL is reachable").
- **Tools:** schema-discovered `netsuite_suiteql`, `netsuite_financial_report`.
- **Output:** findings table + recommended-fix list in prose. Explicitly states it does not
  and cannot post.

## 6. The profile — `financial_analysis.yaml`

Mirrors `bigquery.yaml`'s "senior BI analyst" pattern, for finance.

```yaml
profile_id: financial_analysis
display_name: "Financial Analysis (Controller)"
trigger_tools:
  - netsuite_financial_report
  - metric_compute
prompt_fragment: |
  ## Financial Analysis Context
  You are also a senior financial controller / FP&A analyst. Whenever a financial report or
  a named metric is computed this turn, interpret it through a GAAP-grade lens:
  - Accrual basis + matching: distinguish timing/accrual effects from real economic change.
  - Materiality: focus commentary on movements that matter; don't over-explain noise.
  - Conservatism + comparability: flag one-offs, reclasses, and policy/period inconsistencies.
  Workflow when a report/metric appears:
  1. Contextualize vs the prior period and the relevant benchmark/convention.
  2. Identify the driver(s) of any material movement (volume / price / mix / timing / one-off).
  3. Flag anomalies and data-quality caveats (sign flips, suspense balances, missing periods).
  4. Suggest the relevant deep-dive skill when applicable: /flux, /aging, /ratios,
     /margin-bridge, /cashflow, /close-checklist, /books-review.
  The figures are rendered automatically by the tool — narrate the interpretation; never
  restate or recompute the numbers, and never author your own SQL for a named metric.
rag_partitions: []
```

No hardcoded columns / SQL / tenant schema → no-pollution clean (same shape as `metrics.yaml`,
which documents that short behavioral routing guidance does not violate the rule).

## 7. Menu visibility (free)

The chat composer (`frontend/src/components/chat/chat-input.tsx`) already renders a
data-driven slash-command menu: typing `/` opens a popover that fetches
`GET /api/v1/skills/catalog`, lists each skill's **name + description**, and autocompletes its
primary `/`-trigger. Because the catalog returns *every* registered skill, all 7 new skills
appear automatically after the backend redeploys. **No frontend change required.**
Requirement: each skill's first trigger is a clean slash command and its Name/Description are
menu-quality (concise, action-oriented).

## 8. Trigger hygiene (correctness)

`match_skill` does substring containment on every turn and returns the first slug-sorted
match, so triggers coexist with the recon/period/reporting skills. Rules:
- Every semantic trigger is **≥ 2 specific words** — never a bare `variance`, `margin`,
  `aging`, `ratio`, `cash flow`, `close`, `books` (these would hijack recon / normal turns).
- Exactly one **unique** slash command per skill, globally unique across all skills.
- Proposed triggers (final list refined in the plan; validated by test):
  - `/flux`: `/flux`, `flux analysis`, `p&l variance`, `income statement variance`
  - `/aging`: `/aging`, `ar aging`, `ap aging`, `aging triage`
  - `/ratios`: `/ratios`, `ratio analysis`, `financial ratios`
  - `/close-checklist`: `/close-checklist`, `month-end close`, `month end close`, `close the books`
  - `/margin-bridge`: `/margin-bridge`, `gross margin bridge`, `margin bridge`, `price volume mix`
  - `/cashflow`: `/cashflow`, `cash flow analysis`, `cash runway`, `runway analysis`
  - `/books-review`: `/books-review`, `books review`, `gl hygiene`, `clean up the books`
- A routing test asserts: each phrase routes to its skill; no new trigger hijacks a plain
  finance ask (e.g. "show me revenue") or an existing skill's intent.

## 9. TDD test surface (write tests first)

New `backend/tests/test_financial_skills.py` (+ extend any existing skills/profile test):
1. **Registry:** all 7 new `SKILL.md` parse (have Name/Description/Triggers), load via
   `get_all_skills_metadata()`, and each has a unique primary slash trigger.
2. **Trigger hygiene / routing:** representative phrases route to the intended skill; plain
   finance asks match no new skill; no semantic trigger is < 2 words; slash triggers globally
   unique.
3. **No-prompt-pollution:** every new skill body **and** the profile fragment contain none of
   the pollution markers (`custitem`, `transactionline`, `BUILTIN.DF`, `FROM transaction`,
   `mainline`, `taxline`, …) — assert these are methodology, not SQL.
4. **No-LLM-numbers:** every new skill body contains the standardized output-discipline
   sentinel (delegate to tool, do not restate figures).
5. **Profile:** `financial_analysis.yaml` loads as a valid `KnowledgeProfile`; `trigger_tools`
   include `netsuite_financial_report` and `metric_compute`; `matches_tools({"netsuite_financial_report"})`
   is True; `prompt_fragment` is non-empty and pollution-free.
6. **Catalog exposure (menu):** `get_all_skills_metadata()` includes all 7, each with a slash
   trigger (proves the menu will list them).

## 10. Tier, review, deploy

- **Tier T2** — prompt-pollution surface (chat prompts / knowledge profiles). 
- **Gate:** CI (the tests above) + **blocking pre-merge** `Workflow({name:"code-review-multiangle", args:{target:"feat/advisory-accountant-skills"}})`; fails closed; check `codex_used`.
- **Live smoke:** N/A — no write-path, no customer-data mutation. The recon live-smoke harness
  does **not** apply. Advisory post-deploy dogfood: on staging Framework, invoke each `/`-skill
  and confirm (a) it appears in the `/` menu, (b) it delegates numbers to tools (no restated
  figures), (c) GAAP narration fires. Manual, advisory.
- **Migration:** none.
- **Deploy:** backend-only. Merge to main auto-deploys staging (deploy.yml). Skills/profile are
  baked into the backend image and loaded at import; the FE menu refreshes on reload.

## 11. File manifest

```
backend/app/services/chat/skills/pl_flux_variance/SKILL.md          (new)
backend/app/services/chat/skills/ar_ap_aging_triage/SKILL.md        (new)
backend/app/services/chat/skills/ratio_analysis/SKILL.md            (new)
backend/app/services/chat/skills/month_end_close/SKILL.md           (new)
backend/app/services/chat/skills/gross_margin_bridge/SKILL.md       (new)
backend/app/services/chat/skills/cash_flow_runway/SKILL.md          (new)
backend/app/services/chat/skills/books_review/SKILL.md              (new)
backend/app/services/chat/knowledge_profiles/financial_analysis.yaml (new)
backend/tests/test_financial_skills.py                              (new)
```

## 12. Risks & mitigations

- **Trigger hijack** (a new semantic trigger captures unrelated turns) → ≥2-word triggers +
  routing test that asserts no-hijack against recon/normal phrasings.
- **Number hallucination** (model restates tool figures) → output-discipline sentinel in every
  body + the profile's "never restate" rule + no-LLM-numbers test.
- **Prompt pollution** (a column name slips into a body) → no-pollution test over all bodies +
  the profile.
- **Data-availability gaps** (e.g. no line-level data for the margin bridge, no QBO for books
  review) → each skill degrades gracefully and states the limitation; never fabricates.
- **First-match ordering** (slug order changes which skill wins on overlapping triggers) →
  triggers are disjoint by design; routing test pins the expected matches.
```
