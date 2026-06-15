# Self-Improving Memory & Skills — Program Roadmap

> Date: 2026-06-15 · Status: **active planning**
> Companion spec: [Tenant Memory Graph (①) design](../superpowers/specs/2026-06-15-tenant-memory-graph-design.md)

## Origin

Operator ask: *"self-improving auto-memory saver + skill that grows and self-corrects, per tenant, with an Obsidian-style graph of memory + relationships."* Plus, in the same thread: super-accountant + financial-analytics skills, bookkeeping/accounting automation for NetSuite **and** QuickBooks, and the ability to connect to an **MCP marketplace**.

Two reframes established up front:
1. **No Hermes-per-tenant install.** Hermes is a single-user *desktop* agent with no tenant concept. The multi-tenant **web app already IS the per-tenant runtime** and already implements the Hermes-style auto-memory pattern, keyed by `tenant_id`. The feature lives in the web app, not in N desktop installs.
2. **"Obsidian" = the graph visualization only**, not its storage engine. We want the *graph of concepts + relationships*, built over our own per-tenant stores.

## Ground truth (verified 2026-06-15, adversarially re-checked across working tree + 243 branches + ~40 worktrees)

| Capability | Status | Reality |
|---|---|---|
| **Memory per tenant** | ✅ Built | `TenantLearnedRule` + `TenantQueryPattern` + `TenantEntityMapping` + onboarding profile — `tenant_id`+RLS, injected per-turn, auto-correction extraction (`memory_updater.py`). Plus a **live per-tenant prompt base** (`TenantProfile` → `system_prompt_templates`, RLS). |
| **Skill per tenant** | ⚠️ Net-new | Knowledge-profile YAMLs (13) + agent-skill `SKILL.md` catalog (4) are **global static files**, loaded once at import, **no tenant dimension**. This is the real "skill per tenant" gap. |
| **Super-accountant / financial-analytics skills** | ⚠️ Net-new (cheap) | No accountant/CFO/FP&A persona anywhere. Closest asset: `bigquery.yaml` ("senior BI analyst"). A skill = new `SKILL.md` files + a `financial_analysis.yaml` profile. **Read-only/advisory — needs no posting.** |
| **NS bookkeeping automation** | ⚠️ ~⅔ (read+match+report+lock) | Hardened recon engine, scheduled syncs, four-bucket, materiality routing, evidence packs, period-freeze-as-DB-lock. **Posts nothing**; no JE/AP/AR/accrual/depreciation/statement generation. |
| **"Now we have posting ability"** | ❌ **FALSE** | **No code path posts an accounting transaction to NetSuite at any trust level.** Recon "approve" (REST + chat `recon.approve_match`) = pure DB status flip + audit ("never posts to NetSuite"). Bet 3 Rung 1 envelope = report-only dry-run, default-off. `netsuite_client.py` exposes **only read functions**; no deposit-application, JE, reversal, or dollar-cap config. Codebase is at **Rung 0/1** of the autonomy ladder; posting (Rung 2) is unbuilt and blocked on a **DRAFT operator trust decision**, not engineering. |
| **QuickBooks** | ❌ Absent | Zero code. Named only in two strategy docs as a future "QBO-via-Apideck" target, gated on an `ADR-010` that **does not exist as a file**. Fully greenfield. |
| **MCP marketplace** | ⚠️ Plumbing built, marketplace missing | Generic outbound MCP client (`mcp_client_service.py`) connects to **any** streamable-HTTP server — per-tenant encrypted creds, OAuth2, tool auto-discovery, agent exposure (`ext__…` tools) — already built + tested. Missing: server **catalog**, provider is regex-locked to 4 names, the generic "add connector" UI is **orphaned dead code**, and **the HITL guard only classifies the 4 `ns_*` verbs → writes from any third-party MCP server bypass confirmation** (a security hole to fix before opening up). |

## Phased plan

The throughline: **ship read-only/advisory value now → per-tenant skills + safe self-improvement → only then touch posting.**

### Phase 1 — Visibility + advisory (ships now, zero new write-risk)
- **① Tenant Memory Graph** — the trust/visibility layer (see companion spec). *In design.*
- **Advisory accountant + analytics skills** — new **read-only** `SKILL.md`s (flux/variance on P&L, AR/AP aging triage, ratio analysis, month-end **close checklist**) + a `financial_analysis.yaml` knowledge profile adding GAAP-grade *interpretation* of the existing financial-report tool. The half of "super accountant" that needs **no posting**.
- **MCP catalog (read side)** — lift the 4-value provider regex, wire the orphaned `AddMcpConnectorDialog`, seed a curated server catalog.

### Phase 2 — Per-tenant skills + safe self-improvement (T2, prompt-pollution surface)
- **③ Per-tenant skill layer** — tenant override/extension of the global knowledge-profiles + `SKILL.md` catalog, reusing the existing `TenantProfile`/`system_prompt_templates` pattern as the template.
- **② Passive auto-capture re-enablement** — routed **through** ①'s review surface + eval-gated promotion. **Not** raw live capture (respect the 2026-04-09 pattern-poisoning postmortem); the memory graph is the human-in-the-loop that makes turning it up safe.
- **MCP HITL generalization** — default-deny unknown external write tools (closes the bypass) before exposing untrusted servers.

### Phase 3 — Posting & bookkeeping automation (separate gated track)
- **NS HITL posting (Rung 2)** — deposit application → variance JE → **reversal path** + dollar caps, through the existing HITL gate. *This* is where "posting ability" becomes real. **Blocked on the DRAFT operator trust decision.**
- **Autonomous posting (Rung 3)** — flip the dry-run envelope to actually post, inside caps + kill-switch.
- **QuickBooks** — its own greenfield Apideck-based track (read → write), gated on actually writing `ADR-010`. Largest single net-new lift; the team's own strategy doc warns against day-one QBO.

## Sequencing decision (operator, 2026-06-15)

**Finish ① spec → then advisory accountant skills (Phase 1).** Posting + QB remain separate gated tracks. Posting is a *strategy decision* (operator trust-model, still DRAFT) before it is an engineering task.

## Relationship to the three north stars

This program is **Bet 1 (memory/scalability) realized in the web app** (not the desktop track) + the **advisory** slice of accounting, with posting deferred to **Bet 3**. It does not pull focus off demoable slices: Phase 1 is read-only and shippable.
