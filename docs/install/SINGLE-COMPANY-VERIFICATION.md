# Historical foundation evidence only

This document records the original September 13 local-evaluation foundation at
the revisions listed below. It is superseded as release evidence and does **not**
verify the FW-001 integration baseline, later existing-company adoption, a real
company export/import, production runtime, or any deployment. Use the current
ticket checkpoint and exact-revision CI/independent review for release decisions.

# Single-company foundation — verification, 2026-09-13

Branch: `codex/single-company-foundation`; base: `430d5751`.
Implementation tree before this evidence file: `e8dd2685741c8fdf2e5a5798f12c534fe9aec52e`.
Scope: local evaluation only. No production deployment, public publication,
license change, hosted-tenant conversion or financial write was performed.

## Verified locally

- Docker backend/frontend build; cold database migration to head; interactive
  company/admin bootstrap; API, worker, Beat, Redis and Postgres startup.
- Browser login, dashboard without premature AI onboarding, Settings, and the
  Scheduled Jobs page, using only a synthetic company and administrator.
- 281 backend tests passed: company bootstrap/runtime, authentication/security,
  entitlements, schedule APIs/service/validation, job engine, scheduled-job e2e,
  and the seeded reconciliation lifecycle e2e. Three runtime warnings were emitted.
- Bootstrap concurrency used two independent committed database sessions. Repeat
  bootstrap preserves persisted credentials and feature flags.
- Customer-authored plan creation, approval enforcement, and dispatch passed
  against the HTTP API in tests. Compilation and Celery dispatch were mocked;
  this is not a live AI-provider or real connected-data workflow certification.
- 24 relevant frontend tests; TypeScript check; backend Ruff lint/format;
  frontend lint (existing warnings); 16 workflow guard tests; diff whitespace check.

All database tests targeted an isolated local Postgres instance on port 15439.
The evaluation installation uses separate Docker volumes and generated private
configuration. No existing `.env` was copied into the installation.

## Independent evidence and remaining release gates

Implementer: GPT-6 Astra. Independent static reviewer: Claude Sonnet 5
(`claude-sonnet-5`, confirmed in CLI model usage; CLI bootstrap also used Haiku).
The initial review covered tree `f354af850acf3d750726ee07a6c06e6a94732bb6`.
A subsequent delta review covered concurrency/persisted-row tests, author/approve/
dispatch coverage, onboarding gating, unlimited schedule text and documentation.
It found no new delta defects. The final two Settings presentation changes
(vendor cards and seat-count text) still need coverage in the formal release review.

Reviewer dispositions:

- Kept `check-runtime` in the common entrypoint. It safely no-ops for hosted mode;
  parsing stays consistent with Pydantic's dotenv support and boolean spellings.
  The review noted the additional hosted startup import dependency as a remaining risk.
- Supabase is not certified by this local profile. A runtime Supabase guard was
  not added; production role/access design remains unresolved.
- Bootstrap name behavior is now explicit; stale-object and concurrency test
  gaps were fixed. Soul files remain untouched. Beat bookkeeping is ephemeral;
  application schedule/run history remains in Postgres.

This is **not a merge/release approval**. The prescribed T2 multi-angle Workflow
gate was unavailable in this tool session; the single CLI reviewer is not a
substitute. Full CI (including coverage, complete frontend/SuiteApp suites and
Gitleaks) remains pending. A broad backend run was stopped after 405 passing
tests with no failure; it was incomplete and is not counted as full CI.
The required post-deployment safe live smoke was not run because nothing was
deployed to staging or production.

Production readiness also requires an RLS-enforced runtime database role, TLS,
backup/restore verification, monitoring, and deployment/upgrade/rollback work.
Current scheduling uses the existing typed step registry; a general unattended
custom-agent step has not been implemented.
