---
description: CFO/CEO-grade design standard for EVERY customer-facing dashboard/report/artifact (information design, statement norms, mock-first process). Full reference = .claude/skills/executive-dashboard-design.
paths:
  - backend/app/services/report/**
  - backend/app/mcp/tools/netsuite_financial_report.py
  - backend/app/services/bigquery_service.py
  - backend/app/services/excel_export_service.py
  - frontend/src/app/(dashboard)/reports/**
  - frontend/src/app/(dashboard)/skills/**
  - frontend/src/app/(dashboard)/dashboard/**
  - frontend/src/app/(dashboard)/reconciliation/**
---

# Report & dashboard design standard — CFO/CEO-grade

> Scope: ALL customer-facing output — playbook/chat/refresh reports, the Skills section's outputs, recon dashboards, BI charts, evidence packs/exports. Load the `executive-dashboard-design` skill for the full reference; this file is the terse binding checklist.
>
> The `paths:` list above covers today's known output surfaces — when you build a NEW customer-facing surface, add its path here in the same PR.

Origin: 2026-07-19 redesign session. The first playbook artifact shipped as a raw GL dump (truncated table, no subtotals, no Net Income, meaningless chart) and was rejected on sight. The bar is the approved mock (`cfo-statement-mock-v1` artifact) and this checklist. Benchmarks: Sonar/Dietrich "Executive Command Center" (beat it), NetSuite's own Income Statement Dashboard, IBCS/ISO 24896, CFO board-deck norms.

## Process — visualize before you build (BINDING)

1. **Mock-first**: any new report/dashboard surface (or material redesign) gets an HTML design mock for operator eyeball-approval BEFORE implementation. Use real numbers wherever available; label illustrative data. Mocks using real tenant numbers live in gitignored/scratch locations only — never committed, never in shared artifacts without operator sign-off.
2. **Rendered-artifact acceptance gate**: the slice is not done when tests pass — it is done when the actual rendered artifact is viewed and holds up against this standard. Reviews that check seams but never look at the page have failed before (PR #172).

## Information design (what makes it executive-grade)

3. **Numbers married to judgment** — every headline figure carries context: delta vs prior period, vs same period last year, and margin/% where meaningful. A bare number is an incomplete sentence ("Is $1M good or bad?").
4. **KPI card anatomy**: label → large value → MoM delta (▲/▼ + %) → YoY delta → margin/sub-detail → sparkline. 4-6 cards max above the fold; drill-down carries the rest.
5. **Variance quad** on every comparison table: Actual | Prior | Δ$ | Δ% side by side (NetSuite's own budget-vs-actual layout). When budget data exists, add Budget + variance-to-plan as the third axis.
6. **Statement tables use the two-step GAAP skeleton**: section headers → account detail (indented, muted acct numbers) → bold section subtotals → formula rows (Gross Profit, Operating Income) → Net Income as the emphasized final row. Balance sheet adds the A = L + E check; trial balance the debit/credit in-balance check.
7. **NEVER truncate a financial statement.** Row caps are for ad-hoc query tables only. Long statements scroll/collapse — they do not cut off.
8. **% of revenue (common-size) column** parallel to the $ column; revenue pinned at 100%.
9. **Number formatting**: whole dollars (or $000s with the scale stated once), parentheses for amounts reducing profit, `tabular-nums`, exact Decimal arithmetic server-side — never float-derived presentation.
10. **Color discipline (IBCS)**: red/green are reserved EXCLUSIVELY for favorable/unfavorable variance — never for decoration or scenario identity. Semantic color ≠ tenant accent (white-label `accent_hsl` still applies to chrome).
11. **Charts executives use**: line for trends (the default — NetSuite's own exec dashboard uses only lines), bar/column for structural comparison, waterfall for bridges (always paired with an exact-value table). NO pie, NO radar, NO truncated axes, NO per-account bar dumps.
12. **Watch items / highlights are computed and materiality-gated**: driver-attribution pattern — "[metric] changed by [amount], driven by [named account]" — only for variances crossing a stated threshold. Narrative paragraphs are deterministic templates whose every figure is tool-computed.

## Mechanics (invariants)

13. **No LLM writes a number, ever** — the trust story IS the product. All aggregates/deltas/narrative figures computed server-side; provenance ("Sources & method") block always present.
14. **Self-contained artifacts**: inline SVG only — no CDNs, no chart libraries, no external fonts. The in-app report iframe is EMPTY-sandbox (deliberate security decision — scripts never run there): interactivity is CSS-only (checkbox + `:has()` collapse, SVG `<title>` exact-value tooltips); JS may only ever be a non-load-bearing enhancement for the downloaded standalone file.
15. **Print/PDF is first-class**: board packs are the endgame. `@media print` un-clips scroll regions, keeps subtotal emphasis, hides interactive chrome.
16. **The report stylesheet is %-formatted** — a literal `%` anywhere in the `_CSS` string (including comments) detonates; double every percent (`%%`).
