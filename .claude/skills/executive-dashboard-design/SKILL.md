---
name: executive-dashboard-design
description: CFO/CEO-grade dashboard and report design — information architecture, statement norms, IBCS visual discipline, computed-narrative patterns, and the mock-first process. Use this skill WHENEVER building or reviewing ANY user-facing dashboard, report, playbook output, skills-section output, recon dashboard, BI chart surface, evidence pack, or exported artifact. Trigger on "dashboard", "report design", "KPI", "executive", "board pack", "statement", "chart", "visualization", or any task whose deliverable a customer will look at. The operator's standing directive (2026-07-19)&colon; "something awesome looking and full of informative" — raw data dumps are rejected on sight.
---

# Executive Dashboard & Report Design

## Why this skill exists

PR #172 shipped a technically-perfect playbook report — deterministic, provenance-backed, fully tested through four review layers — that the operator rejected in one glance: a truncated raw GL dump with no subtotals, no Net Income, a meaningless chart, and a robotic one-line narrative. Nobody had looked at the page. The redesign mock (`cfo-statement-mock-v1` artifact, 2026-07-19) was approved with: *"whenever we build something let's make sure we visualize like this."* The competitive bar is Sonar/Dietrich's "Executive Command Center" one-shot artifacts (insight-dense, dark-theme, KPI-rich) — we must look at least that good WHILE keeping what they can't have: deterministic numbers, provenance, versioning, auto-refresh.

## The one-sentence standard

**Every number carries its judgment, every judgment carries its number, and the page reads top-down: verdict → drivers → evidence.**

A bare figure ("$1M revenue") is an incomplete sentence — is that good, bad, on track? Every headline number needs direction (vs prior period), context (vs same period last year, vs plan when available), and proportion (margin, % of total). Conversely, every claim ("costs are rising") must name its number and its driver account.

## Page architecture — the three-tier fold

1. **Above the fold — the verdict.** Watch-item chips (computed, materiality-gated callouts) + 4-6 KPI cards. Card anatomy: uppercase label → large value (`tabular-nums`) → MoM delta with ▲/▼ and color → YoY delta → margin/sub-detail → sparkline with emphasized endpoint. Never more than ~6 cards; drill-down carries the rest.
2. **Mid fold — the drivers.** One trend chart (line, trailing 6-12 periods) + the variance quad table (Actual | Prior | Δ$ | Δ%). When budget data exists, the quad becomes Actual | Budget | Δ$ | Δ% (NetSuite's own budget-vs-actual layout) with MoM/YoY as secondary axes.
3. **Below the fold — the evidence.** The full detail table (statement, aging, run detail…), grouped and subtotaled, NEVER truncated (collapse/scroll instead), then computed highlights, computed narrative, provenance footer.

## Financial statement norms (when the surface is a statement)

- **Two-step GAAP skeleton**: section headers → indented account detail (muted account numbers) → bold section subtotals → formula rows (Gross Profit, Operating Income) → Net Income emphasized (accent row / double rule). Balance sheet adds the A = L + E check row; trial balance the debit/credit in-balance check.
- **Common-size column**: % of revenue parallel to $, revenue pinned at 100%.
- **Number format**: whole dollars (or $000s, scale stated once in a header note), parentheses for amounts reducing profit, exact Decimal server-side — presentation rounding is fine, float arithmetic is not.
- Subtotal labels adapt to sign ("Operating Loss" when negative every period shown).

## Visual discipline (IBCS / ISO 24896)

- **Red/green are reserved exclusively for favorable/unfavorable variance** — never decoration, never scenario identity. Semantic color is separate from the tenant white-label accent (`accent_hsl` themes chrome only).
- **Chart types**: line for any trend (the executive default), bar/column for structural comparison, waterfall for bridges (budget→actual, GP→Net Income walks) always paired with an exact-value table. **Never**: pie, radar, truncated axes, per-account bar dumps, sparklines as primary viz.
- Same scale across compared charts; axes never cut.
- Tables may embed micro-charts (number + shape in one row) — precision and gestalt together.

## Computed insight (the no-LLM narrative)

- **Watch items**: deterministic checks crossing a materiality threshold (house convention: $50 / 1%-class thresholds), rendered as chips with amber/red/green dots. Pattern: metric + number + comparison baseline ("Returns 1.9% of gross sales — trailing-6 avg 1.4%").
- **Highlights**: driver-attribution sentences — "[metric] changed by [amount] ([%]), driven by [named account] ([amount])" — computed from per-account deltas, largest movers first, threshold-gated (don't narrate noise).
- **Narrative**: a deterministic template paragraph whose every figure is tool-computed. The LLM may NEVER write a number into an artifact (`feedback_no_llm_numbers`); insight density comes from computing more (deltas, margins, top movers, concentration, trend position — "best month in trailing 6"), not from prose.

## Mechanics (binding invariants)

- **Provenance always**: the "Sources & method" block (tool label, filtered params, executed_at) + "no model generated a figure" line. This is the differentiator vs every prompt-based competitor.
- **Self-contained**: inline SVG + vanilla JS only — no CDNs, no chart libs, no external fonts (CSP + frozen-artifact durability). The in-app report iframe is EMPTY-sandbox (deliberate security decision — scripts do NOT run there): interactivity must be CSS-only (checkbox + `:has()` collapse, SVG `<title>` exact-value tooltips) with any JS reserved for the downloaded standalone file and never load-bearing.
- **Print/PDF first-class**: board packs are the endgame. `@media print` un-clips scroll regions, keeps subtotal emphasis, hides interactive chrome.
- **The report stylesheet (`report_html._CSS`) is %-formatted** — a literal `%` anywhere in it (comments included) detonates; double every percent.
- Data comes from real sources with real capability: the statement tool returns ordered section prefixes + computed summaries; trend templates (`income_statement_trend`, `balance_sheet_trend`) take comma-separated period lists; prior-period and same-month-last-year are extra recipe sources — comparisons are cheap, use them.

## Process (from CLAUDE.md — binding)

1. **Mock-first**: HTML design mock → operator eyeball-approval BEFORE implementation. Real numbers where available; illustrative data labeled.
2. **Acceptance gate = the rendered artifact**, viewed against this skill — not green tests. Reviews that check seams but never look at the page have failed before.
3. The approved exemplar to match: `cfo-statement-mock-v1` (2026-07-19) — neo-brutalist report DNA (cream ground, 3px borders, hard shadows, Inter 800, accent bar) elevated with KPI row, trend chart, variance quad, collapsible two-step statement, computed highlights/narrative, provenance.

## Applies to

Playbook reports, chat-composed reports, refresh output, the Skills section's outputs, recon dashboards and close packages, BigQuery BI charts, pricing reports, evidence packs / Excel exports, and any future artifact a customer opens. The terse binding checklist auto-loads via `.claude/rules/report-design.md` when editing those paths; this skill is the full reference.
