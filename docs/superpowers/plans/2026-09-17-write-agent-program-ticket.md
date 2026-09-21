# ClickUp ticket to create — target list: Suite Studio AI › Framework Launch (list id 901421078818)
# Priority: high · Start: 2026-09-15 · Created by Claude on 2026-09-16 at Aiden's request; the daily MCP limit blocked the call.

## Name

Self-correcting NetSuite write agent — write kernel program (G1–G7)

## Description (markdown)

# Goal

A NetSuite write agent that can carry out what it proposes. Every approved correction is prepared from fresh evidence, approved exactly by a human, sent at most once, verified by independent readback, and recovered truthfully when anything is interrupted, so that a full accounting group (the 54-order Framework case) completes end to end on staging with every member in a specific final state.

**Acceptance rule:** one complete staging journey, not passing parts. Fresh evidence → correct scope → persisted proposal → exact human approval → execution → independent readback → reconciliation → record and audit links → truthful final status, for a whole group, with queue, preparation, execution, verification and display times measured separately. Every member ends completed or in a specific resumable or blocking state.

**Keep:** exact human approval, tenant/account/role boundaries, exact money, duplicate prevention, independent verification, audit. **Relax:** prescribed investigation sequences and unnecessarily narrow adapters, as validated replacements land.

# Why

Post-mortems of 2026-09-16 (`docs/postmortem/2026-09-16-accounting-agent-end-to-end.md`, `…-group-performance-follow-up.md`): the agent had more freedom to explain than to execute; components disagreed about the same operation; "attempted" was confused with "completed"; preparation was not durable (23 of 54 members hit the 450 s cap); the group summary said 7 reconciled when 30 children had receipts; about one minute per correction was unattributed; acceptance measured parts instead of the journey.

# Goals and state

- **G1 Contracts and registries** — DONE, PR #264. One treatment registry, typed transport, exact-money evidence, declared reconciliation target.
- **G2 Ops digest and kill switch** — DONE, PR #265. Unknown outcomes reach a person daily; one operator switch halts every send.
- **G3.1 Write kernel** — DONE, PR #269 (migration 109). The operations ledger is the kernel: outcomes `executing | rejected_before_effect | committed_unverified | unknown | verified | needs_review`, one-use dispatch permit before any send, readback before verified, DB trigger refuses any downgrade of a receipt.
- **G3.2 Chat accounting cards through the kernel** — PR #270, in CI. Chat approvals are an approval source of the same ledger; recovery from the ledger row; retry lineage.
- **Native amendment adapter** — next. Retires the legacy claim path.
- **G3.4 Group durability** — pulled ahead of G3.3: resumable per-case preparation, group summary derived from child state, per-call timing and cost on the ledger row, receipts off the shared recon queue.
- **G3.3 Generic writes and evidence-driven repair** — GenericRecordAdapter, repair policy in code with per-class budgets, unchanged retries refused, changed meaning needs a new card.
- **G4 Account-aware capability coverage** — adapter limits become account rules; needs a real account's rules (staging read still blocked).
- **G5 Persist progress before scaling** — resume preparation and execution per case; five mixed cases, then the full group.
- **G6 Earn automation** — verified outcomes promoted into reviewed, versioned account playbooks; scheduled detection kept distinct from scheduled correction.
- **G7 Measurement** — correctness, unnecessary interventions, duplicate effects, elapsed time and model cost per verified case; the llmOps page for super admin (traces per turn, tool, hook and provider call) follows G3.

# Records

- Spec: `docs/superpowers/specs/2026-09-15-write-kernel-design.md`
- Review records: PR #266 (kernel, four gate rounds + independent review), PR #267 (cards, four rounds + independent review); superseded by #269 and #270 after rebasing.
- Related tickets: "NetSuite write path is blind" (86bbgnw82), FW-018 (86bc113w6). Close those with this one when the staging journey passes.
