# Real-NetSuite Statement Curation Fix — Design

**Date:** 2026-07-05 · **Tier:** T2 (financial report presentation) · **Ticket:** ClickUp `86bapzbr9`
**Branch:** `fix/report-netsuite-statement-curation`

## Problem

The report-quality stack (Phases 2–5, shipped 2026-07-03 as PR #160) was live-verified by running the **real** Framework Cash Flow Statement (`ns_runReport` id `-203`) through the shipped pipeline. Three real-data gaps surfaced that the synthetic unit tests missed (they used *indented, named* fixtures):

1. **Junk leading row** — the curated statement leads with a meaningless `"Financial Row"` line (NetSuite's placeholder `label` for the unnamed grand-total, whose `value` is `null`).
2. **Incoherent statement** — the curated statement **drops the Investing & Financing sections**. Real statement `reportData` has **no `indentLevel`** keys, so every line is `level 0`; the `_curate_statement` >8 trim then falls to head‑4+tail‑4 (keeps Operating's internals + the conclusions, drops the middle).
3. **No chart** — `_driver_rows` finds no named leaves. Real detail lines (`isDetailLine:true`) are **unnamed** (`value:null, label:null`); the account name sits on the `isDetailLine:false` sibling, which the code classifies `is_summary=True`.

**Root cause:** the shipped code assumes hierarchy comes from `indentLevel` and that `isDetailLine=false ⇒ summary`. Real NetSuite statements provide **neither**.

## Confirmed structural signals

Verified on **three** consolidated FY2025 statements — Cash Flow (`-203`), Income Statement (`-200`), Balance Sheet (`-202`). All share one format:

| Signal | Meaning |
|--------|---------|
| no `indentLevel`/`indent`/`level` key anywhere | hierarchy is **not** indent-based |
| `parent ∈ {null, "finandim_srawfullname", "finandim_srawvalidname"}` | depth: null = top (sections + conclusions), fullname = mid, validname = deep |
| **`label != null`** | a **section / subtotal / total** row (keyed off *presence*, not the string — locale-independent). `label == null` ⇒ account or detail row |
| `value == null` (with `label != null`) | the single unnamed grand-total "junk" row (entry 0), present in all three |
| `label == null` + `value` present + `isDetailLine=false`, immediately followed by an `isDetailLine=true` marker | a **named leaf account** (e.g. `40001 - Sales`). Account *groups* (e.g. `61000 - Facilities` with children) are NOT followed by their own detail marker |

(BS: 27 `label!=null` sections vs 135 named accounts — the discriminator is clean at scale.)

## Design

All changes are **backward-compatible**: new behavior activates only when `indentLevel` is absent (real NetSuite). Synthetic tests inject `line_meta={is_summary,level}` directly and stay unaffected.

### Fix A — flatten carries real hierarchy (`tool_call_results.py`: `_line_hierarchy`, `_extract_report_data_as_table`)
- `level`: keep the existing `indentLevel/indent/level` parse; **when all absent**, derive from `parent` (null→0, `finandim_srawfullname`→1, `finandim_srawvalidname`→2, other non-null→1).
- `is_section` (new `line_meta` field): `label is not None`.
- `is_leaf` (new `line_meta` field): a named `isDetailLine=false` row whose next entry (sorted) is an `isDetailLine=true` marker. Requires one-row lookahead in the flatten.
- The unnamed `isDetailLine=true` marker rows stay in the faithful table (label `""` ⇒ already inert to curation and drivers) — the "never drop a figure" invariant is preserved.

### Fix B — curated statement uses sections, not accounts (`report_service.py`: `_curate_statement`, `_has_summary_lines`)
- Candidate rows = `is_section` (fallback: `is_summary`) **with `value` present** (drops the junk row → fix #1) and a non-null amount. Account rows (`label==null`) are excluded → statement stops being polluted.
- Trim by `level` (now meaningful) → keeps the shallowest sections + conclusions → **coherent** (fix #2).
- During the ≤8 level-trim, **within each candidate level-subset dedupe consecutive equal-amount rows** (a section header and its own total — e.g. Operating Activities = Total Operating Activities — are consecutive *within the level-0 subset*), so an `"X"`/`"Total X"` pair costs one slot, not two, and the coherent level-0 set fits ≤8. Structural only (adjacency + equal amount; no label matching). This is what lets the existing **deepest-level-that-fits** loop land CF on the level-0 section flows (Operating/Investing/Financing) while P&L lands on the meatier level-1 (Income/COGS/Gross Profit) — because CF's level-0 fits after dedup but P&L's level-0 is only the 3 outer totals, so P&L falls through to level-1.

### Fix C — driver chart = true leaf accounts (`report_service.py`: `_driver_rows`, `_auto_chart_section`)
- Drivers = `is_leaf` rows (fallback: non-`is_summary` named rows) ranked by `|amount|`, top‑K, excluding sections/groups → **no subtotal/grand-total double-count / bar-soup** (fix #3).
- `<2` leaves → no chart (existing behavior). The chart is a bar of top leaf accounts for a single-period statement; monthly *trends* still render as a line via the shipped Phase‑4 logic + agent composition.

## Test plan
- Commit **trimmed real fixtures** from CF / P&L / BS `reportData` (structure preserved, row count reduced).
- Assert per statement:
  - **CF**: statement = Operating / Investing / Financing / Net Change / Cash at End (no `"Financial Row"`, no account rows); callouts = trailing conclusions; leaf-driver bar chart contains real accounts (Inventory, A/P…) and **no** `Total`/`Net Change`/`Cash at End` labels.
  - **P&L**: statement includes Gross Profit + Net Income; leaves are the top revenue/expense accounts.
  - **BS**: statement includes Total ASSETS + Liabilities & Equity.
- **All 194 existing report tests stay green** (backward-compat path).

## Non-goals / risks
- Not redesigning the raw faithful table (unnamed detail rows remain, inert).
- Heuristics (`"X"/"Total X"` dedup, `isDetailLine`-pairing leaf test) are NetSuite-format specific; three real statement fixtures mitigate overfitting. Kept robust by keying off `label` **presence** + `parent==null` (not exact alias/label strings) wherever possible.
