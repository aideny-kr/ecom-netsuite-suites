# STATE

What the next session must know without replaying history. Both of us read and write it;
it lives in the repo so it survives a context reset, a new session and another machine.
Update it at the end of every task. **Keep it short enough to read in full before acting:**
history goes to `docs/state/archive/` (the 2026-09-16 version, 605 lines, is there verbatim),
not here. A NOW row names its PR and date; a row older than a week is re-checked before anyone
acts on it.

## GOAL — read this before anything else

**Product.** Automate the daily and monthly accounting routine (reconciliation, close,
reporting) with scheduled jobs and agents that read AND write NetSuite, with a person
approving every financial write.

**The current push (user, 2026-09-30):** the in-app agent should resolve accounting issues
the way Claude + the NetSuite MCP does by hand — find the real cause from our own saved
evidence and the NetSuite document chain, propose the exact create/update in ONE approval
card, and talk less. Mock approved: https://claude.ai/artifact/HPjd5nua1a9p151k1hgkEm.
Measured starting point (Framework staging, 09-29/30): 0 of 6 real resolve-it conversations
ended with an approval card; median reply 366 words; median 147k input tokens per turn
(831k for one order); every turn carries 70 tools and ~57k tokens of fixed context.

**Where we are (2026-09-30).** The write kernel (transaction_ops ledger) is the single write
path. #355 (SuiteQL local first, thinking floor med) and #356 (issue-group breakdown) are live
on staging. #364 (a reconciled order stays reconciled) is ready to merge.

## NOW — in flight

| PR / branch | tier | state (2026-09-30) | blocked on |
|---|---|---|---|
| #364 `feat/reconciled-stays-reconciled` → release | T2 | verify.sh PASS c7a5d6c6; packet review round 5 PASS, receipt in body; CI green | Aiden merges; migration 117 must run on deploy |
| #360 `feat/group-breakdown-staging-ui` → `release/frontend-preserved` | T2 | #356's card on the staging UI line (56156d96, deployed); needs #364's "Reconciled" label ported too | codex / Aiden merge |
| local `feat/changed-after-reconciliation` (79b62dc4, unpushed) | T2 | the "changed after reconciliation" flag, split out of #364 after 4 review rounds | rebuild with the narrow rule (NEXT 4) |
| local `fix/verify-log-session-tag` (5a546b11, unpushed) → release | T2 | verify.sh tags its evidence line `session=$CLAUDE_CODE_SESSION_ID`, which the global loop budget hook (`~/.claude` 67e184a) counts | Aiden: push + PR, then the T2 gate |
| #357 `fix/records-status-query` → release | T2 | Records order-status filter timeout | review |
| #333 `release/recon-excel-group-reliability` → main | T2 | the release integration PR; codex merges into the release branch daily (#358–#363 on 09-29/30) | release owner |
| codex line `codex/framework-launch-integration` (#365, #275) | — | codex's Framework-launch work | codex |

**Staging (re-query before relying on it; codex redeployed three times on 09-30).** Backend was
release 2ba3a713 (contains #355, #356), frontend group-breakdown-56156d9 (e5649dd4 + #356).
Framework's Inc September period review was resuming day by day after the 09-29 NetSuite
auth outage.

## NEXT — ordered, with the why

1. **Count credit memos created from each order's invoice in the reconciliation** (decided
   09-30). 14 of the 20 orders the breakdown calls "adjustment never reached NetSuite" already
   have a credit memo for exactly that amount (e.g. R000227174: CM11788 −4.82 on INV363632);
   the comparison reads only the sales order total and refund-linked credits. Reads: invoices
   created from the SO, then CustCred lines with createdfrom = invoice, batched; register them
   as dependencies so a new credit re-checks the order. Scan engine = codex's active area:
   build on the latest release head.
2. **Fix the breakdown's `source_adjustment_not_in_netsuite` label** (#356 defect): check for a
   matching credit memo inside the existing NetSuite read budget; a match is a new cause,
   "already credited in NetSuite; the check ignores it".
3. **The resolver (the approved mock), per slice:** evidence first (case by order number →
   saved Solidus order → NetSuite chain: SO, invoices, deposits, payments, credits, returns);
   one general propose-NetSuite-change card (exact before/after, dry run, one approval, the
   write kernel, readback), copying how the team booked the same situation (e.g. item 1471
   "Sales Adjustments" → 40050, memo "<order> <Solidus label>"); reply contract (≤ ~60 words,
   numbers only in server cards, tool steps collapsed to one line, no unused source chips, no
   tier box); accounting turns get ~10 tools and schema on demand (≤ 20k fixed tokens);
   a resolve-case benchmark built from cases with known answers gates each change.
4. **The changed-after-reconciliation flag, narrow rule:** flag only when a record's version or
   a refund/credit document changes (Solidus updated_at, NetSuite record ids/updated_at, refund
   and credit document ids and amounts including the refund read's dependency_manifest
   transaction ids); detail omitted by a fallback read is never a change. Start from branch
   79b62dc4 and its four rounds of findings (PR #364 comment 5920747940).
5. Carried from before: the staging write-agent journey (Aiden starts it: staging's NetSuite
   is Framework PRODUCTION), the vs-MCP benchmark baseline is still toolless on main (#205
   closed unmerged), and the Track O decision.

## DECIDED — date · chose X over Y · because

Today's first, then the standing ones in one line each (full reasoning in the 2026-09-16
archive).

- **2026-09-30 · A reconciled order stays reconciled, forever, over reopening on a later
  scan** · because 63 of the 66 reopens in 14 days had no change in either system (the scan
  compared another way), and the user ruled "should not open again". A real later change is
  surfaced separately, never by reopening (#364; database guard migration 117).
- **2026-09-30 · Reconciliation counts ALL credit memos created from the order's invoice, over
  only app-verified ones** · because the team books Solidus adjustments as hand-made credit
  memos (14 of 20 checked) and those are the proof the order is right.
- **2026-09-30 · The resolver asks before closing a case, over closing net-zero cases itself**
  · because trust comes first; switch to automatic later. NetSuite changes always need approval.
- **2026-09-30 · Ship the lock alone and split the change flag, over a fifth review round** ·
  because rounds 2-4 found only detector/list issues and each round's findings shared a shape;
  the doctrine is to stop looping and change the mechanism.
- **2026-09-30 · Detect "changed" from record versions and document identities, never from
  hand-picked content fields or a generic *_complete gate** · because field lists missed a
  variant every round (F1, F5-F8, F10), and `tax_complete` is False on every real report, so a
  generic completeness gate hid real changes.
- **2026-09-30 · Hooks attribute verify runs by a session tag that verify.sh writes, and settle
  ticket debts by a deliberate no-op command, over parsing commands or prose** · because four
  review rounds kept finding shell and markdown edge cases; the shape was "parse text".
- 2026-09-29 · Thinking level floor is `med`, never `low` (#355).
- 2026-09-28 · Sonnet 5.5: forced tool calls run on Sonnet 5 (`_FORCEABLE_MODEL`); blocking
  calls are text-only (#353).
- 2026-09-16 · The transaction_ops ledger IS the write kernel; no fourth ledger.
- 2026-09-16 · Tracing over a LangGraph/LangChain rewrite; one tool-use loop.
- 2026-09-16 · Acceptance is one complete staging journey, not passing parts.
- 2026-09-16 · Rebased work ships as a NEW branch/PR; never force-push.
- 2026-09-16 · The kill switch runs BEFORE the ledger claim for chat cards.
- 2026-08-27 · The HITL guard lives at the DISPATCHER, default-denied.
- 2026-08-27 · NetSuite tools are ALLOW-listed; `_BLOCKED_RECORD_TYPES` stays a deny-list.
- 2026-08-27 · Sandbox environment binding is enforced server-side from the account id.
- 2026-08-27 · A repeating gate shape gets a sibling audit / an unrepresentable fix, not a patch.
- 2026-08-25 · Required NetSuite fields are curated in code (`required_field_registry.py`).
- 2026-08-25 · A resolved `ask_user` slot delegates the field to the human.
- 2026-08-06 · Climb the agentic ladder in order; fan out only if it beats one agent on cost.
- 2026-08-06 · Hooks back every done-claim (`stop_guard.py`), carry state across compaction
  (`compact_snapshot.py`) and hold the stopping rule (`loop_state.py`). Enforcement lives in
  hooks, not in scripts we choose to run.
- 2026-08-05 · One strong agent reviews first; the fan-out gate second. Count distinct defects.
- 2026-08-05 · Every tooling pilot gets a kill rule set in advance.
- 2026-08-04 · Reversals post to the CURRENT OPEN period; periods are never reopened by code.
- 2026-08-02 · `verify.sh` is the loop's exit condition. Process global, knowledge local.
- 2026-08-02 · Prefer a docstring next to the code over a rule in CLAUDE.md.

## DON'T — tried, failed, stop re-proposing

- **Don't hand-pick fields to decide whether evidence changed.** Four review rounds on #364.
- **Don't assume what staging runs.** Codex redeploys often; read the running digest and
  compare files with git before a rollout, and never roll an older build over a newer one.
- **Don't merge docs or anything else to `main` casually:** a main merge auto-deploys staging.
- Don't verify by inspecting; import it, run it.
- Don't trust a gate or packet result without its `target`/`base` (or base/head sha).
- Don't claim "no regressions" without a baseline; don't treat one clean round as done.
- Don't add a rule to CLAUDE.md when a docstring would carry it.
- Don't add a fourth write ledger, a per-kind recipe or a new RESTlet for a write path.
- Don't name a keyword parameter `scope` in transaction_ops; don't leave a copied CTE anonymous.
- Don't force-push, and don't ask to relax that rule.

## OPEN — needs a human, blocking something

- **Merge #364** (Aiden). Migration 117 runs on deploy; port the "Reconciled" label to the
  preserved UI line with #360.
- **ClickUp tickets for #355, #356, #364** (Aiden is creating them; the MCP daily limit blocks
  the session). Once 86bc7eebh is confirmed closed, settle the hook's debt by RUNNING
  `: 'TICKET 86bc7eebh: closed by hand in ClickUp (PR #341, #353, #355)'`; prose does not count.
- **The staging write-agent journey** needs Aiden to start it (Framework PRODUCTION NetSuite).
- **Unverified since 2026-08-28:** the leaked `gh` OAuth token rotation, and three test
  customers in PRODUCTION NetSuite (5803124, 5800803, 5795008) to inactivate.
- **`.gitignore:64` unanchored `memory/`** still shadows tracked frontend files under
  `frontend/src/**/memory/`.
- **Track O: finish or drop?** 22 open majors; nothing blocked by it.
