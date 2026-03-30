# Task Queue

> Ordered backlog of specs for Claude Code to execute. Top = next up.
> Managed by Cowork (PM). Claude Code picks from the top when `active.md` is empty.
> **Start a session with**: `Read tasks/LOOP-INSTRUCTIONS.md` then execute the loop.

---

## How This Works

1. **Cowork** adds specs here after Aiden approves the plan
2. **Claude Code** picks the top item, moves it to `active.md`, and starts execution
3. When done, Claude Code moves the item to `completed/` with results and updates CLAUDE.md
4. **Cowork** reviews on staging, writes validation report in `review/`, and flags issues
5. If issues found → Claude Code picks up the review file and fixes before moving on

---

## Backlog (Priority Order)

### 1. Staging Review Fixes — v1.5 Validation
- **Spec**: Pending Cowork's staging review (will appear in `review/`)
- **What**: Fix any issues found during Cowork's Chrome-based staging validation of v1.5
- **Size**: TBD after review
- **Depends on**: #1 + Cowork staging review

### 2. v1.6 Order-Level Reconciliation
- **Spec**: Not yet written — research complete, needs spec
- **What**: Phase 2 reconciliation: Stripe charges/refunds matched against NetSuite sales orders/customer payments. Canonical models (Order, Payment, Refund, Dispute) already exist. Shopify sync proves the pattern. Gap: Stripe charge/refund sync + NetSuite sales order sync + new matching rules.
- **Size**: Large (multi-day, multi-wave)
- **Depends on**: #1 (v1.5 must be stable first)

### 3. v1.4 Cross-System Intelligence (Roadmap)
- **Spec**: Not yet written
- **What**: Per roadmap — cross-system data correlation, unified entity views across NetSuite + Stripe + BigQuery
- **Size**: Large
- **Depends on**: #2

---

## Adding Items

When adding a new item:
```
### N. [Title]
- **Spec**: path/to/spec.md or "Not yet written"
- **What**: One-line description
- **Size**: Small / Medium / Large
- **Depends on**: List dependencies or "Nothing"
```
