# Advisory Accountant + Financial-Analytics Skills — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 7 read-only advisory accountant/FP&A skills + a GAAP-grade interpretation knowledge profile to the unified chat agent, using the existing skill + profile mechanisms — no posting, no tenant columns, no restated numbers.

**Architecture:** Each skill is a `SKILL.md` methodology playbook (frontmatter `Name`/`Description`/`Triggers` + body) loaded by the existing file-system registry; matched per-turn by `match_skill` (slash or semantic substring, first slug-sorted hit wins) and surfaced to the user by the already-data-driven `/skills/catalog` slash menu. The profile is a YAML injected when `netsuite_financial_report`/`metric_compute` are in the tool list. All figures are delegated to existing tools; bodies carry no SQL/columns.

**Tech Stack:** Python 3.12, pytest, PyYAML; files under `backend/app/services/chat/skills/` and `knowledge_profiles/`. Spec: `docs/superpowers/specs/2026-06-16-advisory-accountant-skills-design.md`.

---

## Execution environment (READ FIRST — worktree gotcha)

This work lives in the worktree `.worktrees/feat-advisory-accountant-skills`. The shared
`backend/.venv` has a `.pth` that resolves `import app` to the **main** checkout. To make the
registry/profile loaders scan the **worktree's** files, pytest MUST run with the worktree's
`backend/` as the working directory (CWD precedence wins). Pattern (separate Bash calls — the
compound `cd X && Y` form is auto-denied in this repo):

```
# call 1 (standalone cd; cwd persists to later calls):
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-advisory-accountant-skills/backend
# call 2:
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_financial_skills.py -v
```

Before first run, symlink the venv + node_modules into the worktree if not present (so the
interpreter exists at the worktree path too):
```
ln -s /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv  /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-advisory-accountant-skills/backend/.venv
```
DB/git/venv commands need `dangerouslyDisableSandbox: true` (sandbox blocks `.git` writes; the
import scan itself is fine in-sandbox). No `.env`, no DB, no migration — these tests are pure
file/registry assertions, so no Docker/Postgres needed.

## File structure

```
backend/app/services/chat/knowledge_profiles/financial_analysis.yaml   (new) — GAAP profile
backend/app/services/chat/skills/pl_flux_variance/SKILL.md             (new)
backend/app/services/chat/skills/ar_ap_aging_triage/SKILL.md           (new)
backend/app/services/chat/skills/ratio_analysis/SKILL.md               (new)
backend/app/services/chat/skills/gross_margin_bridge/SKILL.md          (new)
backend/app/services/chat/skills/cash_flow_runway/SKILL.md             (new)
backend/app/services/chat/skills/books_review/SKILL.md                 (new)
backend/app/services/chat/skills/month_end_close/SKILL.md              (new)
backend/tests/test_financial_skills.py                                 (new) — TDD suite
```

Shared conventions used by every skill body:
- **Output-discipline sentinel** (verbatim, ends every body — the no-LLM-numbers gate keys on
  the substring `do not restate`):
  > The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
- **No `` ```sql `` fences, no tenant columns.** Bodies name *tools*, never columns.

---

## Task 1: Test suite (executable spec, red)

**Files:**
- Create: `backend/tests/test_financial_skills.py`

- [ ] **Step 1: Write the failing test suite**

```python
"""Advisory accountant + financial-analytics skills (Phase 1).

Read-only methodology playbooks: NO embedded SuiteQL/tenant columns
(no-prompt-pollution) and NO restated tool figures (no-LLM-numbers). Surfaced via
the existing skill registry + a knowledge profile, and to the user via the
data-driven /skills/catalog menu.
"""
from __future__ import annotations

import pytest

from app.services.chat.knowledge_profiles.loader import load_all_profiles
from app.services.chat.skills import (
    get_all_skills_metadata,
    get_skill_instructions,
    match_skill,
    reload_skills,
)

# slug -> (primary slash, representative routing phrase)
EXPECTED_SKILLS = {
    "pl_flux_variance": ("/flux", "give me a flux analysis on the p&l"),
    "ar_ap_aging_triage": ("/aging", "run an ar aging triage"),
    "ratio_analysis": ("/ratios", "do a ratio analysis for last quarter"),
    "month_end_close": ("/close-checklist", "walk me through the month-end close"),
    "gross_margin_bridge": ("/margin-bridge", "build a gross margin bridge vs last month"),
    "cash_flow_runway": ("/cashflow", "cash flow analysis and runway please"),
    "books_review": ("/books-review", "do a books review for gl hygiene"),
}

# Tenant-schema / SQL markers that must NOT appear in advisory prompts.
POLLUTION_MARKERS = (
    "custitem",
    "transactionline",
    "inventoryitemlocations",
    "builtin.df",
    "iscogs",
    "```sql",
)

# Plain finance asks that must NOT hijack any new advisory skill.
NEGATIVE_PHRASES = (
    "show me revenue",
    "what were sales last month",
    "list our customers",
    "how much did we spend on marketing",
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    reload_skills()
    yield
    reload_skills()


def _slug_meta(slug):
    return next((s for s in get_all_skills_metadata() if s["slug"] == slug), None)


@pytest.mark.parametrize("slug,expected", list(EXPECTED_SKILLS.items()))
def test_skill_registered_with_primary_slash(slug, expected):
    primary_slash, _ = expected
    meta = _slug_meta(slug)
    assert meta is not None, f"skill {slug} not registered"
    assert meta["name"] and meta["description"]
    slash = next((t for t in meta["triggers"] if t.startswith("/")), None)
    assert slash == primary_slash, f"{slug} primary slash {slash!r} != {primary_slash!r}"


@pytest.mark.parametrize("slug", list(EXPECTED_SKILLS))
def test_skill_body_is_methodology_not_sql(slug):
    body = get_skill_instructions(slug)
    assert body, f"no instructions for {slug}"
    low = body.lower()
    for marker in POLLUTION_MARKERS:
        assert marker not in low, f"{slug} body contains pollution marker {marker!r}"


@pytest.mark.parametrize("slug", list(EXPECTED_SKILLS))
def test_skill_body_enforces_no_llm_numbers(slug):
    body = get_skill_instructions(slug)
    assert body
    assert "do not restate" in body.lower(), f"{slug} missing no-LLM-numbers discipline"


@pytest.mark.parametrize("slug,expected", list(EXPECTED_SKILLS.items()))
def test_semantic_phrase_routes_to_skill(slug, expected):
    _, phrase = expected
    matched = match_skill(phrase)
    assert matched is not None, f"phrase {phrase!r} matched no skill"
    assert matched["slug"] == slug, f"phrase {phrase!r} -> {matched['slug']} not {slug}"


@pytest.mark.parametrize("slug,expected", list(EXPECTED_SKILLS.items()))
def test_slash_command_routes_to_skill(slug, expected):
    primary_slash, _ = expected
    matched = match_skill(primary_slash + " for last month")
    assert matched is not None and matched["slug"] == slug


@pytest.mark.parametrize("phrase", NEGATIVE_PHRASES)
def test_plain_finance_asks_do_not_hijack_new_skills(phrase):
    matched = match_skill(phrase)
    if matched is not None:
        assert matched["slug"] not in EXPECTED_SKILLS, (
            f"plain ask {phrase!r} hijacked {matched['slug']}"
        )


def test_all_slash_commands_globally_unique():
    seen = {}
    for s in get_all_skills_metadata():
        for t in s["triggers"]:
            if t.startswith("/"):
                assert t not in seen, f"duplicate slash {t} in {s['slug']} and {seen[t]}"
                seen[t] = s["slug"]


def test_new_semantic_triggers_are_specific_enough():
    for slug in EXPECTED_SKILLS:
        meta = _slug_meta(slug)
        for t in meta["triggers"]:
            if t.startswith("/"):
                continue
            assert len(t.split()) >= 2, f"{slug} trigger {t!r} too generic (<2 words)"


def test_catalog_exposes_new_skills_for_menu():
    metas = {s["slug"]: s for s in get_all_skills_metadata()}
    for slug, (primary_slash, _) in EXPECTED_SKILLS.items():
        assert slug in metas
        assert any(t == primary_slash for t in metas[slug]["triggers"])


def test_financial_analysis_profile_loads_and_activates():
    profiles = {p.profile_id: p for p in load_all_profiles()}
    prof = profiles.get("financial_analysis")
    assert prof is not None, "financial_analysis profile not loaded"
    assert "netsuite_financial_report" in prof.trigger_tools
    assert "metric_compute" in prof.trigger_tools
    assert prof.matches_tools({"netsuite_financial_report"})
    assert prof.matches_tools({"metric_compute"})
    assert not prof.matches_tools({"bigquery_sql"})


def test_financial_analysis_profile_is_pollution_free():
    profiles = {p.profile_id: p for p in load_all_profiles()}
    prof = profiles["financial_analysis"]
    assert prof.prompt_fragment.strip()
    low = prof.prompt_fragment.lower()
    for marker in POLLUTION_MARKERS:
        assert marker not in low, f"profile fragment contains {marker!r}"


def test_existing_skills_still_load():
    slugs = {s["slug"] for s in get_all_skills_metadata()}
    for original in ("period_comparison", "sales_by_platform", "inventory_check", "csv_import_generator"):
        assert original in slugs
```

- [ ] **Step 2: Run, verify it fails**

Run (from worktree backend, see "Execution environment"): `python -m pytest tests/test_financial_skills.py -v`
Expected: FAIL — the 7 skills + profile don't exist yet (registry/profile/routing assertions fail). `test_existing_skills_still_load` should PASS already.

- [ ] **Step 3: Commit the red suite**

```bash
git add -f backend/tests/test_financial_skills.py
git commit -m "test(skills): failing suite for advisory accountant skills + profile"
```

---

## Task 2: financial_analysis.yaml profile

**Files:**
- Create: `backend/app/services/chat/knowledge_profiles/financial_analysis.yaml`

- [ ] **Step 1: Create the profile**

```yaml
profile_id: financial_analysis
display_name: "Financial Analysis (Controller)"
trigger_tools:
  - netsuite_financial_report
  - metric_compute
prompt_fragment: |
  ## Financial Analysis Context

  You are also a senior financial controller / FP&A analyst. Whenever a financial report or a
  named metric is computed this turn, interpret it through a GAAP-grade lens:
  - Accrual basis + matching: separate timing/accrual effects from real economic change.
  - Materiality: comment on what moves the needle; do not over-explain noise.
  - Conservatism + comparability: flag one-offs, reclasses, and policy/period inconsistencies.

  Workflow when a report or metric appears:
  1. Contextualize the result vs the prior period and the relevant benchmark/convention.
  2. Identify the driver(s) of any material movement (volume / price / mix / timing / one-off).
  3. Flag anomalies and data-quality caveats (sign flips, suspense balances, missing periods).
  4. Suggest the relevant deep-dive skill when it fits: /flux, /aging, /ratios, /margin-bridge,
     /cashflow, /close-checklist, /books-review.

  The figures are rendered automatically by the tool — narrate the interpretation; never
  restate or recompute the numbers, and never author your own query for a named metric.
rag_partitions: []
```

- [ ] **Step 2: Run profile tests**

Run: `python -m pytest tests/test_financial_skills.py -v -k profile`
Expected: PASS for `test_financial_analysis_profile_loads_and_activates` and `..._is_pollution_free`.

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/knowledge_profiles/financial_analysis.yaml
git commit -m "feat(skills): financial_analysis GAAP-grade interpretation profile"
```

---

## Task 3: /flux — P&L Flux / Variance

**Files:**
- Create: `backend/app/services/chat/skills/pl_flux_variance/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: P&L Flux / Variance Analysis
Description: Explains period-over-period income-statement movements — line-level $ and % variances above materiality, attributed to volume, price, mix, or timing/accrual drivers.
Triggers:
  - /flux
  - flux analysis
  - p&l variance
  - income statement variance
---

# P&L Flux / Variance Analysis

You are executing the P&L Flux / Variance Analysis skill. This is read-only and advisory — you never post or modify anything. Follow these steps:

1. **Scope the comparison.**
   - Identify the two periods to compare. If the user named them (e.g. "May vs April", "Q2 vs Q1", "this year vs last"), use those. If only one period is given, compare it to the immediately preceding period of equal length. If none is given, default to the most recent closed month vs the prior month, and state which periods you used.
   - Honor the tenant fiscal calendar already in your context for quarter/year boundaries.

2. **Fetch the figures — never invent them.**
   - Call `netsuite_financial_report` for the income statement for each period (or one comparative report if the tool supports two periods). Do not write a query for standard statements; use the report tool, and discover its available parameters rather than assuming them.

3. **Compute the flux.**
   - For each income-statement line, compute the dollar change (current − prior) and the percent change. Decide what to discuss with a materiality threshold: the greater of any threshold the user stated and **5% of the prior-period line value**. State the threshold you applied.
   - Rank lines by absolute dollar impact and focus commentary on the material movers.

4. **Attribute the driver.**
   - For each material variance, name the most likely driver class and explain it in business terms: **volume** (units/activity changed), **price/rate** (selling price or cost rate changed), **mix** (composition shifted between higher/lower-margin items), **timing/accrual** (a cutoff, accrual, or deferral effect rather than real economic change), or **one-off** (a non-recurring item). Flag where a movement looks like a reclassification or data anomaly rather than performance.

5. **Narrate.**
   - Lead with the headline (margin expanded or compressed, and what drove it). Group the explanation by driver. End with the one or two items the controller should investigate.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
```

- [ ] **Step 2: Run flux's cases**

Run: `python -m pytest tests/test_financial_skills.py -v -k pl_flux_variance`
Expected: PASS (registered, methodology-not-sql, no-llm-numbers, routes).

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/skills/pl_flux_variance/SKILL.md
git commit -m "feat(skills): /flux P&L flux-variance playbook"
```

---

## Task 4: /aging — AR / AP Aging Triage

**Files:**
- Create: `backend/app/services/chat/skills/ar_ap_aging_triage/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: AR / AP Aging Triage
Description: Buckets open receivables or payables by age (0-30/31-60/61-90/90+), computes DSO or DPO, flags concentration risk, and produces a prioritized collections or payment action list.
Triggers:
  - /aging
  - ar aging
  - ap aging
  - aging triage
---

# AR / AP Aging Triage

You are executing the AR / AP Aging Triage skill. Read-only and advisory — you never post, write off, or apply anything. Follow these steps:

1. **Determine AR or AP.**
   - If the user said receivables / collections / "who owes us", do AR. If payables / "what we owe" / vendor bills, do AP. If ambiguous, ask once which one before proceeding.

2. **Fetch the open items — never invent them.**
   - Prefer the standard aging report via `netsuite_financial_report` (an AR or AP aging report) if available. Otherwise use `netsuite_suiteql` over open transactions: first discover the relevant columns from the schema (open balance, due date, counterparty), then query — do not assume column names.

3. **Bucket and measure.**
   - Bucket each open item by days past due into 0-30, 31-60, 61-90, and 90+. Compute the total per bucket and each bucket's share of the total balance.
   - Compute **DSO** for AR (or **DPO** for AP) for the period, and compare to the prior period if available.

4. **Find risk and concentration.**
   - Identify the counterparties holding the largest overdue balances and any single-name concentration. For AR, flag balances aging past terms and any credit-risk signals. For AP, flag bills approaching or past due and any early-payment discounts about to lapse.

5. **Prioritize actions.**
   - For AR: a ranked collections sequence (largest / oldest first, with a suggested next step per account). For AP: a ranked payment plan that protects discounts and avoids late penalties while preserving cash.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
```

- [ ] **Step 2: Run aging's cases**

Run: `python -m pytest tests/test_financial_skills.py -v -k ar_ap_aging_triage`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/skills/ar_ap_aging_triage/SKILL.md
git commit -m "feat(skills): /aging AR/AP aging-triage playbook"
```

---

## Task 5: /ratios — Financial Ratio Analysis

**Files:**
- Create: `backend/app/services/chat/skills/ratio_analysis/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: Financial Ratio Analysis
Description: Computes the standard liquidity, leverage, profitability, and efficiency ratio panel from the balance sheet and income statement, benchmarked against conventions and the prior period.
Triggers:
  - /ratios
  - ratio analysis
  - financial ratios
---

# Financial Ratio Analysis

You are executing the Financial Ratio Analysis skill. Read-only and advisory. Follow these steps:

1. **Scope.**
   - Confirm the period (default: most recent closed period) and whether a prior-period comparison is wanted (default: yes, vs the preceding period).

2. **Fetch the inputs — never invent them.**
   - Call `netsuite_financial_report` for the balance sheet and the income statement for the period(s). If a ratio already exists as a blessed metric, prefer `metric_compute` for it and present the blessed value rather than recomputing.

3. **Compute the panel.**
   - **Liquidity:** current ratio (current assets ÷ current liabilities), quick ratio (excluding inventory).
   - **Leverage:** debt-to-equity, interest coverage (operating income ÷ interest expense).
   - **Profitability:** gross margin, operating margin, net margin, return on assets, return on equity.
   - **Efficiency:** asset turnover, inventory turnover, receivables turnover.
   - Skip any ratio whose inputs are unavailable and say so; never fabricate a denominator.

4. **Benchmark and interpret.**
   - Compare each ratio to its standard convention range (e.g. current ratio typically around 1.5–3; quick ratio at or above ~1; interest coverage comfortably above ~2–3) and to the prior period. Note that healthy ranges are industry-dependent.
   - Call out deterioration, unusual values, and what they imply about liquidity, solvency, and returns.

5. **Narrate.**
   - Lead with an overall health read, then group by liquidity / leverage / profitability / efficiency. End with the ratios trending the wrong way.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
```

- [ ] **Step 2: Run ratios' cases**

Run: `python -m pytest tests/test_financial_skills.py -v -k ratio_analysis`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/skills/ratio_analysis/SKILL.md
git commit -m "feat(skills): /ratios financial-ratio-analysis playbook"
```

---

## Task 6: /margin-bridge — Gross-Margin Bridge

**Files:**
- Create: `backend/app/services/chat/skills/gross_margin_bridge/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: Gross-Margin Bridge
Description: Decomposes the change in gross margin between two periods into price, volume, mix, and cost effects (PVM bridge) from line-level revenue, quantity, and cost.
Triggers:
  - /margin-bridge
  - gross margin bridge
  - margin bridge
  - price volume mix
---

# Gross-Margin Bridge

You are executing the Gross-Margin Bridge skill. Read-only and advisory. Follow these steps:

1. **Scope.**
   - Identify the two periods to bridge (current vs comparison). Default to the most recent closed month vs the prior month if unspecified; state your choice.

2. **Confirm the data is available — never invent it.**
   - A margin bridge needs line-level **revenue**, **quantity/units**, and **unit cost**. Discover the schema with `netsuite_suiteql` first to find those fields for this tenant — do not assume field names. Also pull the gross-margin totals for both periods via `netsuite_financial_report` so you can reconcile the bridge to the reported change.
   - If line-level quantity or cost is not available, say so and fall back to a top-level gross-margin variance (revenue effect vs cost effect only); do not fabricate a full price-volume-mix split.

3. **Build the bridge.**
   - Decompose the margin change into: **price** (selling-price change at constant volume/mix), **volume** (units change at constant price/mix), **mix** (shift between higher- and lower-margin products), and **cost** (unit-cost change). Present it as an additive waterfall from prior-period margin to current-period margin.
   - Reconcile: the four effects must sum to the reported margin change; flag any unexplained residual.

4. **Narrate.**
   - Lead with which effect dominated and why, in business terms. End with the lever most worth acting on.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
```

- [ ] **Step 2: Run margin-bridge's cases**

Run: `python -m pytest tests/test_financial_skills.py -v -k gross_margin_bridge`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/skills/gross_margin_bridge/SKILL.md
git commit -m "feat(skills): /margin-bridge gross-margin PVM bridge playbook"
```

---

## Task 7: /cashflow — Cash-Flow & Runway

**Files:**
- Create: `backend/app/services/chat/skills/cash_flow_runway/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: Cash-Flow & Runway Analysis
Description: Narrates the statement of cash flows (operating/investing/financing) and estimates runway from the cash-balance trend and net monthly burn.
Triggers:
  - /cashflow
  - cash flow analysis
  - cash runway
  - runway analysis
---

# Cash-Flow & Runway Analysis

You are executing the Cash-Flow & Runway Analysis skill. Read-only and advisory. Follow these steps:

1. **Scope.**
   - Confirm the period and whether the user wants the cash-flow narrative, a runway estimate, or both (default: both).

2. **Fetch the figures — never invent them.**
   - Call `netsuite_financial_report` for the statement of cash flows for the period and for the cash-balance trend over the trailing months. Use the report tool, not an ad-hoc query, for the standard statement.

3. **Read the cash flow.**
   - Summarize cash from **operating**, **investing**, and **financing** activities and the net change in cash. Distinguish sustainable operating cash generation from one-off financing/investing swings. Note working-capital effects (AR / AP / inventory) driving operating cash.

4. **Estimate runway.**
   - From the trailing cash-balance trend, estimate the average net monthly burn (or build). If burning, compute runway = current cash ÷ average monthly burn, and state the assumption window. If cash-flow positive, say so and frame the build instead. Never present a runway figure without the burn assumption behind it.

5. **Narrate.**
   - Lead with the liquidity headline (runway in months, or self-funding), then the drivers. End with the biggest cash risk.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
```

- [ ] **Step 2: Run cashflow's cases**

Run: `python -m pytest tests/test_financial_skills.py -v -k cash_flow_runway`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/skills/cash_flow_runway/SKILL.md
git commit -m "feat(skills): /cashflow cash-flow + runway playbook"
```

---

## Task 8: /books-review — Books Review / GL Hygiene

**Files:**
- Create: `backend/app/services/chat/skills/books_review/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: Books Review / GL Hygiene
Description: Read-only bookkeeping review — inspects the general ledger for hygiene issues (suspense/clearing balances, unreconciled accounts, miscoding, cutoff gaps) and recommends adjusting entries. Never posts.
Triggers:
  - /books-review
  - books review
  - gl hygiene
  - clean up the books
---

# Books Review / GL Hygiene

You are executing the Books Review / GL Hygiene skill. This is **read-only and advisory**: you inspect the ledger and *recommend* corrections — you do NOT and cannot post, adjust, or modify any entry. Posting is a separate, human-approved step.

This skill is source-agnostic: review whatever general ledger is reachable through your tools. Today that is the NetSuite ledger; the same method applies to any ledger (for example QuickBooks) once its tools are connected.

1. **Scope.**
   - Confirm the period under review (default: the current open period). Ask if the user wants a full review or a specific area (e.g. just suspense accounts).

2. **Inspect the ledger — never invent balances.**
   - Use `netsuite_financial_report` and schema-discovered `netsuite_suiteql` (discover fields first; do not assume them) to examine:
     - **Suspense / clearing / ask-my-accountant accounts** with non-zero balances that should net to zero.
     - **Unreconciled** bank and control accounts.
     - **Duplicate or likely-missing entries** and out-of-balance subledgers vs the GL control account.
     - **Coding inconsistencies** — amounts posted to unexpected or inactive accounts, or miscategorized expense / revenue.
     - **Cutoff / accrual gaps** — expense or revenue in the wrong period, missing accruals or prepaids.
     - **Impossible balances** — for example negative inventory value, or a debit balance in a liability account.

3. **Recommend fixes.**
   - Produce a prioritized findings list. For each finding, describe the issue, its likely cause, and the **recommended adjusting entry or reclassification** in plain terms (which accounts, direction, and why). Make clear each is a recommendation for a human to review and post.

4. **Summarize.**
   - Lead with the count and severity of issues and the books' overall readiness. End with the entries that must be cleared before close.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. Present recommended entries as guidance only; never claim to have posted anything.
```

- [ ] **Step 2: Run books-review's cases**

Run: `python -m pytest tests/test_financial_skills.py -v -k books_review`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -f backend/app/services/chat/skills/books_review/SKILL.md
git commit -m "feat(skills): /books-review source-agnostic GL-hygiene playbook"
```

---

## Task 9: /close-checklist — Month-End Close (meta) + full green

**Files:**
- Create: `backend/app/services/chat/skills/month_end_close/SKILL.md`

- [ ] **Step 1: Create the skill**

```markdown
---
Name: Month-End Close Checklist
Description: Guides the controller through the standard month-end close sequence — subledger recon, GL hygiene, accruals/cutoff, flux review, balance-sheet recon, and a period-close readiness summary. Advisory only; never performs the lock.
Triggers:
  - /close-checklist
  - month-end close
  - month end close
  - close the books
---

# Month-End Close Checklist

You are executing the Month-End Close Checklist skill. Read-only and advisory — you assess readiness and recommend; you never post entries or lock the period. Walk the controller through this sequence, reporting status for each step and what remains.

1. **Subledger reconciliation.**
   - Confirm AR, AP, bank, and any settlement subledgers tie to the GL control accounts. Use the product's reconciliation engine for Stripe / deposit matching where applicable. Flag unreconciled differences.

2. **Books / GL hygiene** (the `/books-review` method).
   - Clear suspense and clearing accounts, fix miscoding, and resolve impossible balances. List recommended adjusting entries for a human to post.

3. **Accruals, prepaids, deferrals, depreciation; cutoff.**
   - Verify recurring accruals and prepaid amortization are booked, revenue / expense cutoff is correct, and depreciation has run. Flag anything missing as a recommended entry.

4. **Intercompany / eliminations** (if applicable).
   - Confirm intercompany balances net and eliminations are booked.

5. **P&L flux review** (the `/flux` method).
   - Run a variance review of the income statement vs the prior period; investigate material, unexplained movements before close.

6. **Balance-sheet reconciliation.**
   - Confirm each material balance-sheet account is supported by a reconciliation or schedule.

7. **Ratio sanity check** (the `/ratios` method).
   - Sanity-check key ratios for anomalies that suggest a posting error.

8. **Readiness summary.**
   - Summarize what is done, what is blocking close, and the recommended entries outstanding. State clearly that locking the period is a human action taken outside this advisory skill.

For each step, fetch any figures via the existing report / query tools — never invent them.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. Present the checklist status and recommended entries as guidance; never claim to have posted or locked anything.
```

- [ ] **Step 2: Run the FULL suite (all cross-cutting tests now green)**

Run: `python -m pytest tests/test_financial_skills.py -v`
Expected: ALL PASS — including `test_all_slash_commands_globally_unique`,
`test_plain_finance_asks_do_not_hijack_new_skills`, `test_catalog_exposes_new_skills_for_menu`,
`test_existing_skills_still_load`.

- [ ] **Step 3: Run the broader skills/chat regression**

Run: `python -m pytest tests/ -v -k "skill or knowledge_profile or prompt_tool_sync"`
Expected: PASS (no regression in existing skill/profile/prompt-sync tests). Investigate any
failure before committing.

- [ ] **Step 4: Commit**

```bash
git add -f backend/app/services/chat/skills/month_end_close/SKILL.md
git commit -m "feat(skills): /close-checklist month-end close meta-playbook; suite green"
```

---

## Task 10: T2 multi-angle review gate (blocking, pre-merge)

Not a code task — the T2 quality gate (prompt-pollution surface).

- [ ] **Step 1: Compute the branch diff vs main** and run the gate:

```
Workflow({name: "code-review-multiangle", args: {target: "feat/advisory-accountant-skills"}})
```

- [ ] **Step 2: Read `status` FIRST.** `INCOMPLETE`/`PREP_FAILED`/`EMPTY_DIFF` ⇒ re-run (never read a failed run as "0 findings"). Check `codex_used: true` (a real independent model attacked the diff); if `false`, re-run where codex is available before treating the pass as final.

- [ ] **Step 3: Resolve every CONFIRMED + PLAUSIBLE-major + UNVERIFIED finding** (fix, or defer with written rationale) before merge.

---

## Task 11: PR + deploy

- [ ] **Step 1: Push the branch** to `origin` and `framework`.
- [ ] **Step 2: Open the PR** via `curl` + `$(gh auth token)` (the `gh` CLI hits TLS OSStatus -26276 in this environment). Body: summary + spec/plan links + "T2 prompt-pollution surface; gate run + result".
- [ ] **Step 3: After merge to main**, the backend auto-deploys to staging (deploy.yml). No migration. Confirm the deploy is green.
- [ ] **Step 4 (advisory dogfood):** on staging Framework, open the chat `/` menu and confirm the 7 new skills appear; invoke `/flux` and one other, and confirm each (a) delegates numbers to tools (no restated figures) and (b) fires GAAP narration. Manual, advisory — not a blocking gate.

---

## Self-review (plan vs spec)

- **Spec coverage:** 7 skills (§5.1–5.7) → Tasks 3–9; profile (§6) → Task 2; trigger hygiene
  (§8) → Task 1 tests `test_new_semantic_triggers_are_specific_enough` + `_globally_unique` +
  `_do_not_hijack_`; no-pollution (§4.1) → `test_skill_body_is_methodology_not_sql` + profile
  test; no-LLM-numbers (§4.2) → `test_skill_body_enforces_no_llm_numbers`; menu (§7) →
  `test_catalog_exposes_new_skills_for_menu`; TDD surface (§9) → Task 1; tier/review/deploy
  (§10) → Tasks 10–11. No gaps.
- **Placeholder scan:** none — every file body and test is complete inline.
- **Type/name consistency:** slugs, slash triggers, and routing phrases in `EXPECTED_SKILLS`
  match the frontmatter `Triggers` in Tasks 3–9 exactly; `POLLUTION_MARKERS` and the
  `do not restate` sentinel match every body verbatim.
- **Ordering note (verified):** `match_skill` returns the first slug-sorted semantic hit.
  Slug order is `ar_ap_aging_triage, books_review, cash_flow_runway, csv_import_generator,
  gross_margin_bridge, inventory_check, month_end_close, period_comparison, pl_flux_variance,
  ratio_analysis, sales_by_platform`. Each routing phrase in `EXPECTED_SKILLS` contains only its
  own skill's trigger as a substring, so no earlier slug shadows it.
```
