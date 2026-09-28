# Suite Studio development

Use the global task/model preferences. Work directly with one lead unless supporting agents are requested. Keep changes scoped to the request and preserve unrelated working-tree changes.

## Domain knowledge

`.agents/skills` exposes the maintained `.claude/skills` files by relative symlink; edit the canonical files rather than copying them. Use applicable NetSuite/system-specific skills when implementing or diagnosing those systems. Generic SQL or framework knowledge does not override verified account/connector quirks.

For NetSuite queries, read `netsuite-mastery` and its connector contract. Preserve raw status versus display values, transaction/header aggregation grain, fiscal periods, currency context, role visibility, custom fields, governance and connector-specific pagination. Reuse applicable verified evidence; qualify observations by their scope.

For chat internals, use `ai-agent-design`: current architecture uses a unified agent and knowledge profiles. Do not recreate the retired three-tier routers. A development-model upgrade does not authorize changing application model choices.

## Verification and release

Use checks appropriate to the changed behavior and run required CI. Auth, tenant isolation, monetary calculations, write confirmation, idempotency and audit controls remain mandatory. Product skills under `backend/app/services/chat/skills` have a distinct uppercase frontmatter contract; do not convert them into Codex skills.

Before integration/release, read the risk-tier contract in `CLAUDE.md` (UAT + Review) and `.claude/rules/uat-review.md`. Preserve required independent pre-merge review. A clean self-review is not a substitute. Record the reviewed revision and actual implementer/reviewer models; a tool named Codex does not by itself prove cross-model independence. Do not add duplicate advisory review stages.

Read relevant `.claude/rules` when changing their subsystem: auth/data work uses sqlalchemy-fastapi and alembic; chat uses chat-orchestration; frontend uses frontend; reconciliation uses recon-stripe; SuiteScript uses suitescript; deployment uses deploy. Scheduled work also follows agent-graph; reports follow report-design. These are domain/release contracts, not a requirement to invoke a legacy orchestration harness.

Proceed with already-authorized work. Use a design mock when a material design decision needs review; otherwise build and inspect the actual visual result. Publishing, deployments, financial writes and external messages follow the task's authorization and existing enforcement.

## Review packets

Before required review, follow `scripts/review_packet/README.md` to prepare a packet from the clean candidate and actual PR base. Record the task acceptance, actual configuration, evidence and risks. Give the packet to the existing independent reviewer; preserve actual model/revision evidence. Add the generated brief and receipt to the PR description. Regenerate after scope/base/head changes. A packet consistency pass does not replace CI or live acceptance.

## Current handoff

`docs/handoff/2026-09-20-recon-budget-and-tax-proof.md` — read before touching reconcile
budget accounting or the refund tax proof. It carries the shipped state (#277 migration 110,
#278), the 30 Framework Inc cases still reopening, the exact profile write that activates the
tax proof (user's explicit go required), and the answered nexus / Solidus-refund-tax questions.
