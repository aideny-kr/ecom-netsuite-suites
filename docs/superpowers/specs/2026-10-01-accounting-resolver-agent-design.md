# Accounting Resolver Agent — Architecture and Spec

**Date:** 2026-10-01 · **Status:** decisions recorded 2026-10-01 (§9) · **Tier:** T2 (proposes NetSuite writes, closes cases)
**Owner:** Aiden · **Builds on:** `2026-08-19-agentic-netsuite-write-loop-design.md`,
`plans/2026-09-15-self-correcting-write-agent-codex-prompts.md`, mock
https://claude.ai/artifact/HPjd5nua1a9p151k1hgkEm (approved 2026-09-30)

## 1. Goal

**The in-app agent resolves a reconciliation case the way Claude + the NetSuite MCP does by
hand:** it finds the real cause from our saved evidence and the NetSuite document chain,
then either explains that NetSuite is already right or proposes the exact create/update in
one approval card. It talks briefly. A person approves every write and every case close.

### Done means (a predicate, not an aspiration)

Measured on the resolve benchmark (section 7), held-out tasks, the agent as deployed:

| # | Criterion | Target | Today |
|---|---|---|---|
| G1 | Outcome-correct: right diagnosis **and** right action (exact payload, or "already right", or "escalate") | ≥ 85% pass@1, ≥ 75% pass^3 | ~0% (0 of 6 live conversations reached an approval card) |
| G2 | Not worse than the reference: Claude (same model family) + the same MCP tools in a plain loop, same tasks | ≥ reference on G1 | no reference run yet |
| G3 | Safety: executions without approval; writes differing from the approved payload; writes to an environment the session did not choose | 0 / 0 / 0 | the third is not enforced (§5.6) |
| G4 | Brevity: median reply words; amounts written by the model | ≤ 60; 0 | 366 median; amounts in prose |
| G5 | Cost: fixed context on an accounting turn; median tokens per resolved case | ≤ 20k; ≤ 150k | ~57k fixed; 831k for one order |
| G6 | Live: real Framework cases on staging resolved end to end (approved card + verified readback, or approved "close as explained") | ≥ 10, by a person other than the builder | 0 |

G1–G5 are checked by code; G6 by the case ledger. Nothing is "done" on a narrated success.

## 2. Why we failed so far (measured 2026-09-29/30, Framework staging)

1. **Workflow-shaped tools with a fixed menu.** The agent can fix only the 7 kinds in
   `transaction_ops/treatments.py` (`KINDS`). Outside that menu it stops: "no supported fix".
2. **No evidence-first path.** For R000227174 it queued 4 recon runs, asked the user for the
   Solidus figures, and spent 831k tokens. The saved evidence already held the answer, and
   NetSuite was already right (CM11788 −4.82 on INV363632).
3. **No general "propose this exact NetSuite change" card.** Writes exist only per fix kind.
4. **Overloaded context.** 70 tools (~74k characters of schemas, half the external NetSuite
   MCP incl. report/app widgets) and ~57k tokens of fixed context on every turn, including
   turns with no tools.
5. **Prompt pollution.** Source chips, a tier box, a confidence footer, and a soul rule to
   "always show the SQL". Median reply 366 words; one reply claimed reports it never ran.
6. **No feedback loop.** No benchmark of resolving cases with known answers, so no change
   could be shown to help. (The vs-MCP chat benchmark measures Q&A, not resolution.)

Claude Code + the NetSuite MCP succeeds on the same cases because its harness is the
opposite on each point: a few general tools, context loaded when needed, free exploration
of the document chain, a human approving exact writes, and terse output.

## 3. What Anthropic and OpenAI do (and what we take)

| Practice (source) | What we take |
|---|---|
| An agent is a model using tools in a loop; start with one agent and add structure only when measured to help (Anthropic *Building effective agents*; OpenAI *A practical guide to building agents*) | One resolver loop inside the existing unified agent. No new specialist agents (our DECIDED "one unified agent"). |
| Separate the **brain** (stateless harness + model) from the **hands** (tools/MCP/sandboxes, `execute(name, input) → string`) and the **session** (durable append-only event log outside the context window) (Anthropic *Scaling Managed Agents*, 2026-04-08) | Our capabilities become an **Ops MCP server** (hands) that any brain can drive: our orchestrator, Claude Code, Codex. The case file is a durable log, not chat history. Credentials stay server-side. |
| A few high-signal, consolidated, namespaced tools; return meaningful names, not ids; paginate/filter by default; actionable errors; descriptions written for a new teammate; evaluate tools with real multi-step tasks (Anthropic *Writing effective tools for agents*) | ~8 resolver tools (§5.2) replace the workflow menu. `case_open` returns one compact case file instead of ten calls. |
| Context is a finite budget: load just in time, compact, keep notes outside the window; tool search instead of all schemas up front (≈85% fewer tool tokens); keep big intermediate results out of the window (programmatic tool calling, code execution with MCP) (Anthropic *Effective context engineering*, *Advanced tool use*, *Code execution with MCP*) | Accounting turns load ≤ 10 tools; the rest via tool search. Domain knowledge as on-demand skills, not injected YAML. Group work computes in code, not in the transcript. |
| Guardrails at three points (input, output, tool); put validation **next to the tool that creates the side effect**; risk-rate tools; high-risk actions pause for a human; approval is an interruption with resumable state; MCP `require_approval` per tool (OpenAI *Guardrails and human review*; Agents SDK) | Our dispatcher guard, HMAC cards and write kernel already are this. Add risk ratings, the environment binding (§5.6), and keep "a repaired payload needs fresh approval". |
| Evals: tasks with success criteria, several trials, graders that check the **outcome** (end state), not the transcript's claim; read transcripts; held-out sets (Anthropic *Demystifying evals for AI agents*) | The resolve benchmark is built **first** (slice S1) and gates every later slice. |

## 4. Architecture

```
             ┌──────────────────────── BRAIN (stateless) ────────────────────────┐
 user ──────►│ orchestrator: one tool-use loop, frontier model per task           │
 (chat,      │  • accounting-turn profile: ≤10 tools + tool search, skills on     │
  case page) │    demand, output contract, budgets + exit reasons                 │
             └───────────────┬───────────────────────────────┬──────────────────┘
                             │ execute(name, input)           │ events
             ┌───────────────▼──────────── HANDS ─────────┐   ▼
             │ Ops MCP server (ours; tenant-scoped)       │  SESSION / CASE FILE
             │  case_open · evidence_read · chain_read    │  durable append-only log:
             │  netsuite_query · schema · skill_find      │  evidence refs, findings,
             │  propose_change · close_as_explained       │  hypotheses, cards, outcomes
             │  ── guardrails live HERE, at the tool ──   │  (survives compaction,
             │  NetSuite REST/SuiteQL · Solidus/BigQuery  │   resumes after approval)
             │  · saved recon evidence · write kernel     │
             └───────────────┬────────────────────────────┘
                             │ write proposals only
             ┌───────────────▼────────────────────────────┐
             │ APPROVAL + WRITE KERNEL (existing)          │
             │ validator/repair (08-19) → one exact card → │
             │ human approves → transaction_ops ledger →   │
             │ NetSuite write → independent readback       │
             └─────────────────────────────────────────────┘
             EVALS: resolve benchmark replays real cases through the same MCP
             (our brain vs Claude + same MCP) and grades the outcome.
```

**Why an Ops MCP server:** the same hands serve every brain. That gives an honest
benchmark (§7: same tools, different brain), lets Claude Code or Codex resolve a case
when our agent cannot, and puts every guardrail where the side effect happens, not in a
prompt. The in-app orchestrator stays the product's brain.

## 5. Components

### 5.1 The resolver loop (brain)

- Runs inside `chat/orchestrator.py` as an **accounting-turn profile**, not a new agent.
- **The loop is model-driven, with phases shown as checklist state, not as a router:**
  investigate → diagnose → act (explain, propose, or escalate) → verify.
- **Model: Claude Opus 5.5 (`claude-opus-5-5`) at high thinking for complex issues (decided
  §9 D2).**
  - A turn is complex when it starts from a case or issue group, or when the conversation
    already has a case open. This is decided by code, not by a classifier, so it isn't routing.
  - Other turns keep the tenant's default model (Framework: `claude-sonnet-5`).
  - Thinking is never below `med`.
  - **Check in S3 before relying on it:**
    - the forced-tool and text-block contracts (Sonnet 5.5 broke them, #353;
      `_FORCEABLE_MODEL`);
    - that Framework's own key (BYOK) has access to Opus 5.5;
    - the cost per resolved case against G5.
- **Budgets live in run state:** steps ≤ 40 (existing), tokens per case ≤ 300k (hard), and
  stall detection (the same failing call or empty result twice ends the loop).
- **Exit reasons:** `resolved | explained | escalated | budget | stall | blocked | error`.
  Never a bare stop.

### 5.2 Tools (hands): the Ops MCP server

| Tool | Risk | Returns / does |
|---|---|---|
| `case_open(order_ref \| case_id)` | read | One compact **case file**: Solidus order and adjustments, NetSuite chain summary (SO, invoices, deposits, payments, credits, returns, refunds with ids, numbers, amounts), the balance and its metric deltas, the breakdown cause, prior fixes, related cases. Readable names, not just ids. |
| `evidence_read(case, part)` | read | The full saved evidence for one part, paginated. |
| `chain_read(record)` | read | Live NetSuite document chain around a record, using the SuiteQL forms known to work (memory: `transactionline.createdfrom`; not the link tables that return 500). |
| `netsuite_query(sql)` / `netsuite_schema(table)` | read | Bounded read-only SuiteQL (engine-checked, never regex). Schema is loaded on demand, not injected. |
| `skill_find(case)` | read | The skill for this case's cause (§5.5), with its checks run on the case's own evidence: the skill and its change template, or no skill with the failed check named. Seeded from how the team books each situation, e.g. a Solidus adjustment → credit memo against the invoice, item 1471 → 40050. |
| `propose_change(record_type, op, payload, rationale, evidence_refs)` | **high**: never executes | Runs the 08-19 validator and repair, does a dry run, and returns **one exact card**: before/after, environment, accounts, amounts from the server. It replaces the per-kind menu; the 7 existing kinds become skills (§5.5) and validators. |
| `close_as_explained(case, reason, evidence_refs)` | **high**: never executes | A card. The user decided closing a case asks first (2026-09-30). |
| `verify_after(write)` | read | Independent readback and re-comparison after an approved write. |

Every tool is tenant-scoped and namespaced (`ops_*`). Errors say what to do next, e.g.
"missing required: subsidiary; call `netsuite_schema('creditMemo')`".

The external NetSuite AI Connector MCP stays available. On accounting turns it is
**deferred** (loaded through tool search), so its report and app widgets don't sit in context.

### 5.3 Context

- **Fixed context on an accounting turn is ≤ 20k tokens:** a short system prompt, the
  output contract, ≤ 10 tool schemas, and the case file once opened.
- **Domain knowledge is loaded as on-demand skills:**
  - Framework booking conventions;
  - SuiteQL dialect and gotchas;
  - reconciliation semantics (refunds vs credits, reconciled-is-final);
  - fiscal calendar.

  The knowledge-profile YAML becomes the source for these skills; the two copies stay
  verbatim-synced per CLAUDE.md.
- **Soul and prompt hygiene:** no SQL echo, no source chips on accounting turns, no tier box.
  The soul is never edited without Aiden's explicit OK; this spec only proposes the change.
- **Compaction:** after long turns, keep the case file and findings log, and drop raw tool
  dumps.

### 5.4 Output contract

- ≤ 60 words of prose, at most one card per turn, and tool steps collapsed to one line.
- **Every number comes from the server**, in a card or table (our existing interception);
  the model never writes amounts.
- **Each turn ends in one of three shapes:**
  - "NetSuite is right because …" with a close-as-explained card;
  - "Here is the change" with a change card;
  - "I need X" with exactly one question.

### 5.5 Skills (learned procedures; decided 2026-10-05, D5)

A **skill** is a proven fix for one cause, written down so the agent can apply it to every
case with that cause. Example: "a Solidus adjustment never reached NetSuite → a credit memo
against the invoice". It replaces today's fixed menu of fix kinds, one of the measured
reasons the agent failed (§2).

- **What a skill holds:**
  - **when it applies:** checks on the case file and the live chain (`case_open`,
    `chain_read`), e.g. "an order adjustment equals the difference, and no credit memo
    created from the invoice already covers it";
  - **the change it proposes:** a payload template whose amounts, items and accounts come
    from that case's evidence, never from the skill;
  - **how to verify:** the readback after approval, and what "resolved" means.
- **Format:** a versioned file per skill, loaded on demand like Claude's skills. It is
  not injected into every prompt.
- **Where skills come from:**
  - seeded from the team's booking conventions (e.g. item 1471 → 40050, memo
    "<order> <label>");
  - then drafted by the agent once the same cause has been resolved **and verified**
    several times (an approved write whose readback verified, or an approved
    close-as-explained).
- **Aiden approves every new skill or skill change.** This keeps the 2026-04-09 rule
  that nothing is learned from live sessions automatically. It follows the 09-15 plan's
  "learned repairs"; "precedents" in earlier drafts are this skill evidence.

### 5.6 Guardrails (where the side effect happens)

- **Existing, kept:**
  - the dispatcher guard (default-denied, allow-listed NetSuite tools);
  - HMAC cards;
  - "a repaired payload needs fresh approval";
  - the kill switch before the ledger claim;
  - the transaction_ops ledger (one write path, idempotency);
  - readback verification;
  - reconciled cases never reopen (migration 117).
- **Risk ratings per tool**, as in §5.2. High-risk tools return a card and never execute.
- **Prerequisite, decided 2026-08-27 but NOT built:** environment binding enforced at the
  dispatcher. `tools.netsuite_environment_of` is display only today. Framework staging's
  NetSuite is PRODUCTION, so this lands before the resolver can propose writes there.

### 5.7 Group resolution (decided 2026-10-05, D5)

The agent resolves a whole issue group, not one case at a time. The Inc "Order
differences" group is 46 orders but four problems; the eight $59 touchpad gaps are one fix,
eight times.

1. **Split by cause.** The group breakdown (#356) partitions the members.
2. **Apply the cause's skill to every member, on that member's own evidence.** The
   skill's checks run per member. A member that is already credited, or whose amounts,
   tax or documents differ, **drops out with its reason** and is handled on its own. A
   batch never carries a member whose evidence does not fit.
3. **One approval card per batch** lists every line: order, document, amount and
   account. The HITL invariants hold unchanged: nothing posts without approval, every
   line is audited, and period freeze and environment binding apply per line. Amounts are
   computed in code, never by the model.
4. **After approval, each line is verified on its own** (readback). A line that fails
   does not mark the batch resolved; it reopens alone.
5. **Each verified line is skill evidence** (§5.5).

## 6. What stays decided (do not reopen)

- One unified agent; no routing (relaxed only for picking an existing READ workflow).
- The transaction_ops ledger is the write kernel; there is no fourth ledger.
- Every financial write needs a person's approval; the HITL guard sits at the dispatcher.
- Never present tool-computed numbers as model text.
- Reconciled is final; a credit memo created from the invoice counts (#364, #371).
- The resolver asks before closing a case.
- Tracing over a framework rewrite (no LangGraph or LangChain).

## 7. The resolve benchmark (built first)

- **Tasks:** at least 40 real Framework cases with known answers. 25% are held out and
  never used while building.
  - 20 "adjustment never reached NetSuite" orders. 14 are already right via credit memo,
    so the answer is "explain + close". 6 need labelled answers.
  - The Inc "Order differences" group (46 orders, four situations):
    - $0-invoice orders that are already right;
    - business orders;
    - the recurring $59 touchpad gap;
    - one-offs.
  - Tax-only refunds blocked by refund_audit (34).
  - Reopened-then-locked cases.
  - Sandbox (SB1) write tasks with a gold payload.
- **Gold labels, per cause (§9 D4, amended 2026-10-05):**
  - The builder groups tasks by **fact signature**, never by the app's diagnosis: the
    difference amount, an order adjustment equal to it, customer type, invoice total, and
    credits already in the chain.
  - Aiden labels one or two cases per group directly, then confirms which members share
    the label. Members that differ are labelled on their own.
  - Each inherited label records which label it came from.
  - The sheet still carries **no suggested answer**, so the labels stay independent of the
    agent being measured.
- **Gold label per task:**
  - the diagnosis category;
  - the expected action: explain/close, change (record type, op, key fields, accounts,
    amounts) or escalate;
  - the evidence that proves it.
- **Graders:**
  - **Code checks the outcome:** the diagnosis, a payload diff against gold, and the end
    state on SB1 after approval.
  - **Code checks safety and cost:** cards, words, model-written amounts, tokens, tool calls,
    wall time.
  - **A model-graded rubric** for clarity.
  - **Weekly human read of failing transcripts.**
- **Group tasks (§5.7):** a group passes only when:
  - every member is right;
  - every member that differs was caught as an exception;
  - the group needed one approval per batch.
- **Trials:** 3 per task; report pass@1 and pass^3.
- **Reference:** Claude in a plain loop with the same Ops MCP and NetSuite MCP. This replaces
  the toolless baseline problem (#205) for this domain.
- **Runs on every PR that touches the resolver** (CI, held-in set) and nightly (full set).

## 8. Slices (each gated by the benchmark; work blocks in `plans/2026-10-02-accounting-resolver-blocks.md`)

| Slice | Builds | Exit criterion |
|---|---|---|
| S0 | Environment binding at the dispatcher; risk ratings (blocked on a sandbox connector, which needs Aiden's OAuth; must land before S4, not before reads) | Tests prove a write to the unchosen environment is refused |
| S1 | Resolve benchmark on "Order differences" first: tasks, labelling sheet, Aiden's gold labels, graders, runner; baseline runs of today's agent and the Claude+MCP reference | Numbers for both on the held-in set |
| S2 | Ops MCP server: read tools first (`case_open`, `evidence_read`, `chain_read`, `netsuite_query/schema`, `skill_find`) | Reference agent on the new tools ≥ reference on raw tools |
| S3 | Resolver profile in the orchestrator: ≤10 tools + tool search, skills, output contract, case file log, budgets | Our agent's G1 on held-in ≥ reference; G4, G5 met |
| S4 | `propose_change` + `close_as_explained` cards through validator/kernel; `verify_after` | SB1 end state matches gold; G3 = 0 |
| S5 | Skills and group resolution (§5.5, §5.7): skill format seeded from booking conventions; group runner (split by cause, per-member checks, exceptions out, one card per batch, per-line verify); agent-drafted skills reviewed by Aiden | Held-in group tasks pass (every member right, exceptions caught, one approval per batch); G3 = 0 |

## 9. Decisions (Aiden, 2026-10-01)

| # | Chose | Over | Because |
|---|---|---|---|
| D1 | **Our orchestrator** as the brain, with our capabilities built as an **Ops MCP server** | Claude Agent SDK harness; Anthropic Managed Agents | It keeps multi-provider models, our approval cards and tenant scoping. Claude Code or Codex can drive the same MCP later, and it is the benchmark's reference. |
| D2 | **Claude Opus 5.5 at high thinking for complex issues** (a turn started from a case or issue group, or with a case open); other turns keep the tenant default | Fable 5.1; a benchmark-picked model; Sonnet 5 for everything | Aiden's call: Opus 5.5 high is the resolver brain. Cost is measured by G5. |
| D3 | **"Order differences" first**: the Inc group (46 orders, four situations) and the 20 "adjustment never reached NetSuite" orders | Tax-only refunds first; a mix | The answers are mostly known, so gold labels are cheap and trustworthy. |
| D4 | **Aiden labels every task**; amended 2026-10-05: **per cause** (one or two per fact-signature group directly, then confirms members; differing members on their own) | The builder labels and Aiden spot-checks; agent labels; every task one by one | Independent gold labels, with no suggested answer on the sheet. Per cause matches how the agent will resolve (D5). |
| D5 | **The agent resolves whole groups with skills** (2026-10-05): a skill per proven cause, applied to every member on that member's own evidence, one approval card per batch | One case at a time; a fixed fix-kind menu | 46 Inc orders are four problems. Per-member checks and per-line verification keep it safe; Aiden approves every skill. |

## 10. Not building

- New specialist agents, a router, or a framework rewrite.
- Autonomous writes or auto-close.
- Learning from unverified sessions.
- More fix kinds: skills (§5.5) replace the menu.
- A second write path that bypasses the kernel.

## Sources

- Anthropic, *Building effective agents* — https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, *Writing effective tools for AI agents* (2025-09-11) — https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, *Effective context engineering for AI agents* — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, *Introducing advanced tool use* — https://www.anthropic.com/engineering/advanced-tool-use
- Anthropic, *Code execution with MCP* — https://www.anthropic.com/engineering/code-execution-with-mcp
- Anthropic, *Scaling Managed Agents: decoupling the brain from the hands* (2026-04-08) — https://www.anthropic.com/engineering/managed-agents
- Anthropic, *Demystifying evals for AI agents* — https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- OpenAI, *A practical guide to building agents* — https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/
- OpenAI, *Guardrails and human review* — https://developers.openai.com/api/docs/guides/agents/guardrails-approvals
- OpenAI Agents SDK, *MCP* — https://openai.github.io/openai-agents-python/mcp/
