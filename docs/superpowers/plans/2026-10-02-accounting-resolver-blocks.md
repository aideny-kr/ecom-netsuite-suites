# Accounting Resolver — work blocks

**Spec:** `specs/2026-10-01-accounting-resolver-agent-design.md` (PR #377) · **Date:** 2026-10-02

Each block is one PR or one person-task, with its own acceptance check. Blocks inside a
phase can run in parallel unless a dependency is listed. Measuring comes before building:
no brain or hands block merges without benchmark numbers to compare against.

## What already exists (checked 2026-10-02)

- **Write validator and repair loop (08-19 design) are built:** `chat/write_validator.py`,
  `write_validation.py`, `record_metadata_service.py` and `write_repair_bound.py`. The
  change card reuses them.
- **Write kernel:** `transaction_ops` ledger, `chat_confirmation.py` (the chat card as an
  approval source), `executor.py` (approved execution plus independent verification).
- **Benchmark infrastructure:** `services/benchmarks/` (runner, Claude-baseline runner,
  scorer, persistence). It grades Q&A answers; resolving cases needs outcome graders.
- **`app/mcp/server.py`** is an internal tool registry, not a network MCP endpoint. The
  benchmark reference gets the same tools through the API directly. A real MCP transport
  for outside brains is a later block (B15).
- **Environment binding:** the 08-27 spec is "not started; blocked on a provisioned sandbox
  connector". Every app connector points at production today, so the binding gates the
  **write** blocks (Phase D), not the first blocks.

## Phase A: Measure (first)

| Block | Builds | Owner | Depends | Acceptance |
|---|---|---|---|---|
| **B1 Task set + labelling sheets** | ~60 "Order differences" cases (the Inc group and the 20 adjustment orders, deduped). For each: saved evidence, the NetSuite chain and the breakdown cause. No suggested answer. Delivered as a labelling page that saves each label. | builder | — | Every task has a complete evidence pack; held-out 25% split fixed by stable hash |
| **B2 Gold labels** | For each task: diagnosis category, expected action (explain/close · exact change · escalate), proving evidence | **Aiden** | B1 | ≥ 40 labelled |
| **B3 Resolve-bench harness** | Task loader; **record-once, replay-after** NetSuite reads (fixtures, so runs never hit production or rotate the app's single-use token); our-agent runner (headless orchestrator); graders: diagnosis, action, payload diff, safety, brevity, model-written amounts, tokens; 3 trials, pass@1/pass^3; persisted results | builder | B1 | Runs end to end on 3 sample tasks; graders unit-tested |
| **B4 Reference runner + first numbers** | Claude Opus 5.5 (high) in a plain loop with the same tools, same tasks. Baseline today's agent. | builder | B2, B3 | Numbers for both on held-in |

## Phase B: Hands (read tools)

| Block | Builds | Owner | Depends | Acceptance |
|---|---|---|---|---|
| **B5 `case_open`** | Compact case file from saved evidence: Solidus order and adjustments, NetSuite chain summary with names, balance and deltas, breakdown cause, prior fixes, related cases | builder | — | Fixture tests on real-shaped reports; ≤ 4k tokens per case |
| **B6 `chain_read` + `netsuite_query`/`netsuite_schema`** | Live chain via the SuiteQL forms known to work; engine-checked read-only SQL; schema on demand | builder | — | Tests; R000227174's chain matches the hand reading |
| **B7 `precedent_find`** | Seeded with the team's booking conventions (e.g. Solidus adjustment → credit memo from the invoice, item 1471 → 40050, memo "<order> <label>"); later fed by B14 | builder | — | Returns the convention for an adjustment case |
| **B8 Tools gate** | The reference runner uses B5–B7 instead of raw tools | builder | B4–B7 | Reference G1 on the new tools ≥ on raw tools |

## Phase C: Brain (resolver profile)

| Block | Builds | Owner | Depends | Acceptance |
|---|---|---|---|---|
| **B9 Complex-turn profile** | Code rule: started from a case or issue group, or with a case open → Claude Opus 5.5 at high thinking. Accounting tools ≤ 10; the external NetSuite MCP is deferred to tool search. Checks: forced-tool contract (#353), Framework BYOK access, cost per case. | builder | B8 | Tests for the rule; the three checks recorded |
| **B10 Context + output contract** | Skills on demand (booking conventions, SuiteQL gotchas, reconciliation semantics); ≤ 60 words, one card, steps collapsed; no chips or tier box on accounting turns. A proposed soul change goes to Aiden. | builder · **Aiden OK on soul** | B9 | G4 met on held-in; fixed context ≤ 20k |
| **B11 Case file log + budgets** | Durable case session log; compaction; ≤ 300k tokens per case; stall detection; exit reasons | builder | B9 | Tests; G5 met on held-in |
| **B12 Brain gate** | Our agent on the benchmark | builder | B9–B11 | G1 on held-in ≥ reference |

## Phase D: Act (cards; writes)

| Block | Builds | Owner | Depends | Acceptance |
|---|---|---|---|---|
| **B13a SB1 connector on staging** | A sandbox NetSuite connection in the app (OAuth) for write tasks | **Aiden** (OAuth) | — | Connector active; account id SB1 |
| **B13b Environment binding + risk ratings** | 08-27 spec at the dispatcher; risk rating per tool | builder | B13a | Tests: a write to the unchosen environment is refused |
| **B14a `propose_change` card** | General change card through the existing validator/repair and the kernel; dry run, before/after, environment and server amounts; replaces the 7-kind menu (the kinds become precedents and validators) | builder | B12, B13b | SB1 write tasks: the end state matches gold; G3 = 0 |
| **B14b `close_as_explained` + `verify_after`** | Close card (asks first); readback after approved writes | builder | B12 | Tests; held-in explain tasks close correctly |

## Phase E: Learn, open up, go live

| Block | Builds | Owner | Depends | Acceptance |
|---|---|---|---|---|
| **B14c Precedent capture** | Saved only after a verified write or an approved close; review before use | builder · **Aiden reviews** | B14a | Repeat situations use fewer tool calls |
| **B15 MCP transport** | Expose the Ops tools over real MCP with tenant auth, so Claude Code or Codex can drive them | builder | B8 | A Claude Code session resolves a held-in task through it |
| **B16 Group mode** | One card per proven situation over a group; maths in code | builder | B12 | Only if the data shows per-case work is the bottleneck |
| **B17 Live acceptance (G6)** | 10 real Framework cases resolved end to end on staging | **Aiden** + builder | B14a/b, deploy | 10 cases in the ledger with approved cards and verified readbacks |

## Critical path and parallel work

```
B1 ─► B2 (Aiden) ─► B4 ─► B8 ─► B9 ─► B10/B11 ─► B12 ─► B14a ─► B17
B3 ───────────────┘     ▲
B5, B6, B7 ─────────────┘          B13a (Aiden) ─► B13b ─┘
```

**Start now:** B1, B3, B5 and B6 in parallel. **Aiden:** B13a (sandbox OAuth) any time;
B2 once B1 delivers the sheets.

## Coordination

- Codex is active in the reconciliation scan engine (`transaction_ops`). Resolver blocks
  live in new modules plus `chat/`; read paths only touch scan code through public
  functions.
- Every block is T2 (resolver prompts, write cards, financial reads) except B1/B3 data
  tooling (T1). Every block keeps the review-packet process.
