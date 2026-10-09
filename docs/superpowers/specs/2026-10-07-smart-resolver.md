# Smart resolver: the model resolves, generic guards keep it safe, the outcome verifies it

Date: 2026-10-07 · Owner: Aiden · Supersedes the per-kind builder path of spec 2026-10-01 (#377) §5.5

## Why

Aiden, 2026-10-07, after approving R231821517's credit on staging:
> "this is still very much one by one fix, i think the native MCP will do this way better more efficiently…
> can we get on the right track to smart resolver."

Today every fix type needs two pieces of hand-written code:
- a builder that constructs the exact payload;
- a verifier that proves the posted record against what that builder expected.

The result:
- **One by one.** An unpaid invoice, another adjustment label, a VAT refund, a rounding cent: each needed new code (#396, #400).
- **Fragile.** The verifiers are written against human-created records, so they break on the agent's own records. Example: CM12127 posted correctly but stayed "needs review" because NetSuite omits `taxTotal` on a no-tax credit (#401).
- **Blocked.** `tax_correction.review_for_card` refuses every credit-memo create, sales-order update, invoice discount and tax update that doesn't match a pre-built typed candidate. The native-MCP route that Claude uses well by hand is closed for exactly the records a resolution needs.

## What

### 1. The model does the resolving

The in-app agent works the case like native Claude with the NetSuite MCP. It:
- reads the case and the live documents (chain read, SuiteQL, record metadata, saved evidence);
- decides the fix;
- drafts the exact NetSuite writes itself: create or update, on any allowed record type.

There are no per-type builders. Each case may need more than one write, for example a credit memo and a sales-order update so the sales order and invoice match (Aiden, 10-06).

Skills become **playbooks the model reads**: how the team books each cause, with real examples. They are retrieved for the case. They are guidance, not code paths.

### 2. Generic guards: the same for every fix type

A write that names a case goes through one **case-write guard**, run before the card and again at approval:

| Guard | Rule |
|---|---|
| Record types | Allow-list for resolutions: `creditMemo` (create), `salesOrder` / `invoice` (update) first. System record types stay blocked as today. |
| Scope | Entity, subsidiary and currency equal the case's own order documents, read fresh. A write cannot touch another customer or subsidiary. |
| Idempotency | The server stamps `externalId = digest(tenant, case, intent, target)` on creates and searches for an existing credit by that external ID, by the order number in the memo, and by the same invoice. A second identical write is refused. |
| Period | The posting period is open and AR is not locked. |
| Predicted outcome | The card shows the case before and after: Solidus vs NetSuite total, tax and refunds, computed from the payload. A write that does not reduce the case's difference is flagged on the card. |
| Human | Every write still needs Aiden's approval of the exact payload, as today. |

Typed candidates that exist today (tax reallocation, credit line reallocation, the sales credit) stay as **fast paths**. They take precedence when they match, and are retired once the generic path beats them.

### 3. Verification checks the outcome, not a template

After an approved write executes:
1. **Readback.** `ns_getRecord` the created or updated record, and diff the approved fields against what NetSuite stored. Absent-when-zero fields like `taxTotal` count as zero.
2. **Re-reconcile.** Run the case's own single-order reconciliation (`RunCreate(order_references=(ref,))`).
3. **Result on the card:** "Verified: case reconciled", with the record link and the corrected report, or "Posted as approved; the case still differs by X", with the new report and the next step.

The reconciliation engine becomes the one verifier for every fix type. A model mistake can't be called verified, because the case either balances or it doesn't.

### 4. Measured against native MCP before it replaces anything

- **Harness:** the resolve benchmark from #389 runs both agents on the same cases.
- **Cases:** the cases Aiden labels, plus a staging sample.
- **Scored on:** correct fix, approvals needed, cases reconciled after approval, tokens and time.

The generic path replaces a fast path only when it matches or beats native MCP on that path's cases.

## Slices

1. **Generic case write** (credit memo create; sales order and invoice update). The case-write guard replaces the hard refusal when no typed candidate matches, and the card shows the predicted outcome.
2. **Outcome verification.** Readback diff plus a single-order re-reconcile after every case write. The card shows the verified result, the record link and the corrected report.
3. **Playbooks.** The model reads the playbook for the case's cause. The seed is the unbooked Solidus adjustment; the sales order and invoice must match.
4. **Benchmark** against native MCP on labelled cases, before switching any fast path off.

## Not in scope

- Auto-posting: every write stays human-approved.
- Deleting records.
- Journal entries, until a guard for them exists.
