# Ecom NetSuite Suites — Project Intelligence

> Read by Claude Code at session start. Encodes patterns, conventions, and decisions.

## Development Workflow — FOLLOW ALWAYS

- **TDD strictly**: Write failing tests FIRST, then implement. No production code without a failing test. This applies to EVERY task, regardless of whether the plan document explicitly says "TDD" — assume TDD unless the task is purely non-code (docs, config, infra).
- **Max 15 iterations per task**: Use the loop protocol. If blocked after 3 self-heal attempts within a task, stop and report. An "iteration" = one test-implement-verify cycle, OR one review-fix cycle. Review loops (spec + quality) count toward the 15.
- **Multi-agent execution**:
  - *Sequential implementers* — fresh subagent per task via `subagent-driven-development`. Never dispatch multiple implementation subagents in parallel against the same working tree (file conflicts).
  - *Parallel reviewers* — spec-reviewer + code-quality-reviewer run after each task (sequential within a task but fast).
  - *Parallel research* — use Explore / general-purpose agents in parallel when investigating independent questions.
  - *True parallel implementation* — only when tasks touch disjoint file sets (e.g., backend-only vs frontend-only) AND file ownership is pre-declared in the plan AND each runs in its own worktree.
- **Subagent dispatch checklist** — every implementer prompt I send must include:
  1. TDD required (write failing test first)
  2. Max 15 iterations per task; stop and report if blocked after 3 self-heal attempts
  3. Research existing code before writing
  4. Commit when green; one commit per logical change
  5. Files owned by this task (to prevent conflicts with other subagents)
  Subagents do NOT inherit `CLAUDE.md` — these rules must be in the dispatch prompt every time.
- **Zero regressions**: Run full test suite before committing. Fix CI as a follow-up after every deploy.
- **Discuss before fixing**: Always discuss approach AND research existing code before making changes.
- **Commit frequently**: One commit per logical change. Never amend. Push to BOTH repos (`origin` + `framework`).

## Architecture Invariants

- **Multi-tenant**: All tables have `tenant_id`. RLS via `SET LOCAL app.current_tenant_id` (use `set_tenant_context()`).
- **NetSuite Auth**: OAuth 2.0 PKCE; per-connection `client_id` (REST + MCP integration records). Never global.
- **Connection Setup**: REST API needs Account ID, Client ID, RESTlet URL (`metadata_json.restlet_url`). MCP needs Account ID, Client ID (separate Integration Record). Collected in `step-connection.tsx`, editable in `netsuite-connections-section.tsx`.
- **File Cabinet I/O**: `ecom_file_cabinet_restlet.js` does in-place load → set `.contents` → `.save()`.
- **SuiteQL paths**: Local REST (`netsuite_suiteql`) supports `customrecord_*`; external MCP (`ns_runCustomSuiteQL`) only standard tables.
- **Chat**: One unified agent. Domain context via YAML knowledge profiles (`backend/app/services/chat/knowledge_profiles/`). Model self-routes.
- **MCP write safety (HITL)**: `mutation_guard.classify_mutation()` intercepts; user approves via `WriteConfirmationCard`; HMAC token; system record types blocked.
- **Fiscal Calendar**: `tenant_configs.fiscal_year_start_month` injected as `## FISCAL CALENDAR` prompt block.
- **White-Label Branding**: Per-tenant brand_name/color/logo in `tenant_configs`; `BrandingProvider` injects `--primary`.
- **Feature Flags**: `tenant_feature_flags` table; `require_feature(flag_key)` returns 403 when disabled.
- **Soul config**: file-based at `/tmp/workspace_storage/{tenant_id}/soul.md`. NEVER overwrite/seed without explicit user confirmation.
- **react-resizable-panels v4**: Imports: `Panel`, `Group as PanelGroup`, `Separator as PanelResizeHandle`. Uses `orientation` prop (not `direction`).

## Key File Locations

| What | Where |
|------|-------|
| Knowledge profile loader | `backend/app/services/chat/knowledge_profiles/loader.py` |
| Prompt assembler | `backend/app/services/chat/prompt_assembler.py` |
| Tool inventory + category registry | `backend/app/services/chat/tool_inventory.py`, `tool_categories.py` |
| Mutation guard + write confirmation | `backend/app/services/chat/mutation_guard.py`, `write_confirmation_service.py` |
| Capability-sync CI invariant | `backend/tests/test_prompt_tool_sync.py` |
| Permission helpers | `backend/app/core/dependencies.py` |
| API client (frontend) | `frontend/src/lib/api-client.ts` |
| SSE chat stream normalizer | `frontend/src/lib/chat-stream.ts` |
| Specs / Plans | `docs/superpowers/specs/`, `docs/superpowers/plans/` |
| Architecture memory | `memory/` |

## Common Mistakes — Universal

1. **Two databases locally** — `.venv/bin/alembic` → Supabase (remote). Docker → `postgres:5432` (local). After adding columns, also run `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head`.
2. **MCP CRUD requires HITL guardrails** — `ns_createRecord`/`ns_updateRecord` MUST NOT auto-execute. Payload preview → confirm → for updates show before/after via `ns_getRecord` → audit log. System record types blocked via `_BLOCKED_RECORD_TYPES` in `mutation_guard.py`.
3. **Unified agent prompt MUST stay in sync with profile YAMLs** — SuiteQL dialect rules live in both the unified agent prompt AND `knowledge_profiles/*.yaml`. Copy verbatim, never paraphrase.
4. **One unified agent, no routing** — all queries go to UnifiedAgent. Domain context via knowledge profiles. No specialist agents, no agent registry.
5. **Benchmark against native Claude + MCP** — every change to chat/agent/profile code must pass the vs-MCP benchmark. CI gate on PRs.
6. **Never let LLM present tool-computed numbers** — LLMs hallucinate/round. Use `_intercept_tool_result` → SSE `data_table`/`task_output`. Condensed result tells LLM "table shown automatically, do NOT list numbers."
7. **Soul config is sacred** — file at `/tmp/workspace_storage/{tenant_id}/soul.md`. NEVER overwrite/seed without explicit user confirmation.

## Known Issues

1. **LLM pivot limitation** — always use `pivot_query_result` tool, not CASE WHEN SQL.
2. **Proven patterns** — auto-learning from live sessions DISABLED (2026-04-09). Only admin-seeded or nightly-promoted patterns are retrievable. 6 verified shipping-country patterns + 1 RAG chunk seeded for Framework.
3. **Stripe initial sync is slow** — 400K+ payout lines, takes 30+ min first time. Batch commits every 200 lines. Hourly incremental via Beat after that.
4. **Agent benchmark vs MCP baseline** — our north star. Every change to chat/agent code must match or beat Claude+MCP. CI gate enforces this on PRs. Nightly cron tracks trends. See `memory/feedback_benchmark_vs_claude_mcp.md`.

## Skills Reference

Domain knowledge lives in `.claude/skills/`. Use the Skill tool to load when needed:

| Skill | Use For |
|-------|---------|
| `netsuite-mcp-chat` | Chat orchestration, knowledge profiles, tool interception, SSE streaming, entity resolution |
| `ai-agent-design` | Knowledge-driven agent architecture, YAML profiles, prompt assembly, benchmarks |
| `netsuite-mastery` | SuiteQL dialect, SuiteScript 2.x, REST API, OAuth, NetSuite tribal knowledge |
| `netsuite-reconciliation` | Reconciliation engine, order-level matching, data pipeline, Stripe sync, evidence packs |
| `bigquery-bi` | BigQuery BI agent, schema seeder, chart pipeline, connector lifecycle |
| `pricing-agent` | Currency conversion, PricingEngine, TemplateFiller, TaskFileService |
| `saas-deployment` | Docker, GCP, nginx, CI/CD, Alembic migrations, staging deploy procedures |
| `suitescript-engineer` | SuiteScript development, workspace, SDF, deploy pipeline |
| `autonomous-improvement` | Nightly eval/experiment loop, scoring, pattern promotion |
| `shopify-ops` | Shopify sync pipeline, order ingestion |
| `netsuite-ai-connector-instructions` | Oracle NetSuite AI Connector setup, MCP integration record |
| `netsuite-owasp-secure-coding` | SuiteScript secure coding (OWASP categories, server-side patterns) |
| `netsuite-sdf-project-documentation` | SDF account-customization project structure and conventions |
| `netsuite-sdf-roles-and-permissions` | NetSuite role + permission reference for SDF deploys |
| `netsuite-suitescript-records-reference` | SuiteScript record API surface (fields, sublists, methods) |
| `netsuite-suitescript-upgrade` | SuiteScript 2.0 → 2.1 migration guide |
| `netsuite-uif-spa-reference` | NetSuite UIF / SPA framework reference |

## Path-Scoped Rules

Claude Code auto-loads matching rules from `.claude/rules/` when editing files in their declared paths:

| Rule | Loads When Editing |
|------|--------------------|
| `alembic.md` | `backend/alembic/**` |
| `sqlalchemy-fastapi.md` | all backend Python (`backend/app/**`, `backend/tests/**`) |
| `chat-orchestration.md` | chat pipeline (`backend/app/services/chat/**`, `backend/app/mcp/**`, chat APIs) |
| `frontend.md` | `frontend/src/**`, `**/*.tsx`, `**/*.ts` |
| `recon-stripe.md` | reconciliation + ingestion + Stripe workers |
| `suitescript.md` | `suiteapp/**` |
| `deploy.md` | workflows + compose + Dockerfiles + infra |

