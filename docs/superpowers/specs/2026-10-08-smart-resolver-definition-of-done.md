# Smart resolver: definition of done

Date: 2026-10-08 · Owner: Aiden

**Builds on two specs:**
- `2026-10-01-accounting-resolver-agent-design.md` (PR #377): this adopts its §1 targets G1–G6.
- `2026-10-07-smart-resolver.md` (PR #403): this sets its slice order.

## Why

Aiden, 2026-10-08:
> "let's have the definition of done so we can evaluate. The bugs, we should catch as we go during the development cycle."

The trigger: R231821517's credit CM12127 posted correctly on staging, but the card stayed "needs review". NetSuite leaves empty money fields out of a credit created through the API. Every test fixture was hand-written and always had those fields, so the bug surfaced only after a live approval (fixed by #401).

## 1. The program is done when all of these hold

Each is measured, never narrated.
- **Measured by code:** G1–G5, from the resolve benchmark on held-out cases (#377 §7).
- **Measured from the case ledger:** G6 and G7, from live staging cases.

| # | Criterion | Target | Today (2026-10-08) |
|---|---|---|---|
| G1 | **Outcome-correct:** the right diagnosis **and** the right action (the exact writes, or "NetSuite is already right", or "escalate") | ≥ 85% pass@1, ≥ 75% pass^3 on held-out cases | Not measurable yet: the harness (#389) is unmerged, and labelling is in progress |
| G2 | **Not worse than native:** Claude plus the same NetSuite MCP in a plain loop, on the same cases | ≥ reference on G1 | No reference run yet |
| G3 | **Safety:** executions without approval; writes that differ from the approved payload; writes to an environment the session didn't choose; **a wrong or duplicate write reaching an approval card** | 0 / 0 / 0 / 0 | The last one is guarded by #403's outcome check, and its review found 2 such paths, both fixed |
| G4 | **Brevity:** median reply words; amounts written by the model | ≤ 60; 0 | 366 median (09-30) |
| G5 | **Cost:** fixed context on an accounting turn; median tokens per resolved case | ≤ 20k; ≤ 150k | about 57k fixed; 831k for one order (09-30) |
| G6 | **Live:** real Framework cases resolved end to end on staging, approved by someone other than the builder. Resolved means one of: an approved card, a verified readback and the case reconciled; or an approved "NetSuite is right" close. | ≥ 10 | 0 |
| G7 | **Verification never fails a correct write:** live approvals of a correct write that end "needs review" | 0 | 1 (CM12127, fixed by #401, recheck pending) |

**"Resolved" means the end state, not the card.**
- The approved write is read back and matches what was approved.
- The case's single-order reconciliation then shows Solidus equal to NetSuite, in total and tax.
- The card shows "verified", with the record link and the corrected report.

A card that was approved but never verified does not count.

## 2. Every slice is done when

These gates catch bugs during development. Each item exists because a bug reached staging without it.

1. **Tests fail first.** Every guard has a test that fails when the guard is removed.
2. **Fixtures come from real records.** For each NetSuite record type the slice creates or updates:
   - read at least 3 real records of that type, created the same way (through the API where any exist), read-only;
   - run the slice's readback and verification on them;
   - keep their shape (including omitted fields) as fixtures.

   Hand-written fixtures alone don't count, because they share the builder's assumptions. CM12127's bug is the example.
3. **Record mode on real cases.** On the held-in cases the slice targets, the agent runs with writes off.
   - Every case that should produce a card produces one, and that card passes approval-time revalidation.
   - Every case that shouldn't produce a card doesn't.
4. **Full check and review:**
   - `scripts/verify.sh` passes on the final head against the release base;
   - independent review runs until a round finds no wrong-write path, with at most 5 rounds;
   - findings that only fail safe become tickets.
5. **Live:** after deploy, one approval per new write shape ends verified, with the case reconciled. Until then the slice isn't done.
   - A failure here reopens the slice. It isn't a new bug ticket.
   - Its fix must add the missing real-record fixture from gate 2.

## 3. Slices, in order

| Slice | Builds | Status | Done when |
|---|---|---|---|
| A | **Credit memo create:** the agent proposes lines, amounts and a reason; the server builds the exact payload and accepts it only if the order then equals Solidus | Built; #403 is a draft | §2 gates. Gate 2 means the readback runs on existing API-created credits such as CM12127. Gate 5 means one live approved credit, verified and reconciled. |
| B | **The resolve benchmark** (#377 S1): harness #389, Aiden's labels, a baseline of today's agent and of native Claude plus the MCP | Harness unmerged; labels in progress | G1–G5 numbers exist for both agents on held-in cases. **Nothing after this can be judged without it.** |
| C | **Sales order and invoice update, so both match** (Aiden, 2026-10-06) | Not started | §2 gates. On held-in cases that need it, the sales-order total, invoice total and Solidus are all equal after one approval round. |
| D | **One verifier for every write:** readback diff, single-order re-reconcile, and a card result with the record link and corrected report | Per fix type today | Every write kind ends through the same verifier, and G7 holds |
| E | **Playbooks** the model reads, one per cause | Seed playbook approved | Held-in cases of each cause pass using the playbooks, with no new code per cause |

**B moved ahead of C:** #377 made the benchmark the gate for every slice. Slice A was built before it, so A can be tested live but not yet scored against native MCP.

## 4. Decisions

| Date | Chose | Over | Because |
|---|---|---|---|
| 2026-10-07 | The agent proposes the **content** (record, lines, amounts, reason); the server builds the exact payload and accepts it by outcome. **Confirmed by Aiden 2026-10-08**, along with the G1–G7 targets. | The agent drafting raw NetSuite payloads (the 10-07 spec's wording) | The server can then enforce scope, idempotency and the outcome check on every write. It's still not per cause: any line set that makes the order equal Solidus is accepted. |
| 2026-10-07 | Review rounds end at a round with no wrong-write path. Round 5 is the last, decided before it runs. | Looping until a round is clean | PR #403: 4, 4, 3, 2, then 1 finding, and one round's fix caused the next round's regression |
| 2026-10-06 | When both the sales order and the invoice are wrong, fix both so they match | A credit memo alone | A credit fixes only the billed side, and reconciliation compares the sales-order total |
| 2026-10-08 | Bugs are caught in the development cycle (§2 gates 2, 3 and 5), not by live approvals | Fixing them as tickets after deploy | Aiden, 2026-10-08 |

## 5. How we evaluate

- **G1–G5:** the resolve benchmark report (#389 CLI). It runs on every PR that touches the resolver (held-in) and nightly (full set). Both agents are reported side by side.
- **G6–G7:** a case-ledger query on staging (approved accounting cards → readback status → case status). Its output is pasted into STATE.md with the date it was run.
- **Per slice:** each PR body lists the §2 gates with evidence for each: test names, the real records read, the record-mode case list, the verify sha and review rounds, and the live case once deployed.
