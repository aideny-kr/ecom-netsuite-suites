# Batch NetSuite writes — idempotency first, then the review surface

**Design reference (approved 2026-08-31):** [`batch-write-review-mockup-reference.html`](./batch-write-review-mockup-reference.html)
· published copy: https://claude.ai/code/artifact/9a7a2e27-482c-4fba-95bb-4b8464f8763d

That mock is the anti-drift anchor. When implementation and mock disagree, decide it
deliberately and update both in the same commit — do not discover it later in a screenshot.

## The problem, stated honestly

A 20-row spreadsheet is 20 cards and 20 approvals today. Nothing batches: the payload,
the HMAC token, the CAS claim, the card and the execution are each strictly single-record.

The obvious shortcut — *approve once, auto-approve the rest* — was considered and
**rejected**, because the approval would be granted before the payloads exist. That is
consent to content nobody has seen, unbounded in count, record type, field values and
account. `agent-graph.md` #11 already names the trap: an HMAC token proves payload
integrity, **not human freshness**.

This session supplied the counterexample rather than a hypothetical. The duplicate bug
fixed in PR #210 — a timed-out write reported as `failed`, with the repair loop offering
the identical payload — was survivable **only because a human was present to reject it**.
Sandbox customer `5264348` already existed. Under auto-approve that becomes a duplicate,
silently, and the same logic fires on every subsequent row.

Batch review costs the same single click and keeps the approval attached to rows the
human actually saw.

| | clicks | records seen |
|---|---|---|
| today — one card per record | 20 | 20 |
| blanket auto-approve | 1 | 0 |
| **batch review** | **1** | **20** |

## Order of work — and why this order

### Phase 1 — Idempotency + side-effect log (no user-visible change)

**Ships alone, before any batch UI exists.** It is independently valuable: it fixes the
single-record path too, where a timeout already leaves an unanswerable question.

`agent-graph.md` #10 requires "a work-derived idempotency key and a side-effect log
written *before* the call" and records that none of it is built. That is the blocker.

- **Key derivation is work-derived, never random**: `sha256(batch_id, row_index,
  canonical_payload)`. A retry of the same work yields the same key; a genuinely
  different write yields a different one. A random key would make every retry a new
  write, which is the defect wearing a disguise.
- **Side-effect row written BEFORE the call**, status `attempted`, then updated to
  `written` / `rejected` on a definite answer. A crash between send and confirm leaves
  `attempted` — which is the true state, and the state the system currently cannot
  represent.
- **Reconcile on resume**: query NetSuite by key (or by the record's natural identity
  where NetSuite offers no key support) and settle `attempted` from evidence. Never
  from a guess, never by retrying blind.
- **`kill -9` mid-write drill is part of the acceptance**, per `agent-graph.md` #12:
  recovery code that has never run is not recovery code.

### SETTLED 2026-09-01 — measured against live sandbox `6738075-sb1`, not documentation

**There is no header channel, and we do not need one. `externalId` gives us idempotency
enforced by NetSuite itself.**

*Evidence 1 — the MCP tool surface has no key slot.* `ns_createRecord` accepts exactly
`recordType` and `data`; `ns_updateRecord` adds `recordId`. Nothing else. Read from the
live server's own schema (`discover_tools`, 16 tools). So a transport-level idempotency
header is not available to us regardless of what REST may support elsewhere.

*Evidence 2 — `externalId` is settable and queryable.* The catalog exposes `externalId`;
SuiteQL returns the column as `externalid`. **Note the case split** — the catalog is
camelCase, the SuiteQL column is lowercase. This is the identical trap documented in
`required_field_registry.field_value`, where lowercase rule names silently missed every
real write. Any lookup must be case-insensitive.

*Evidence 3 — NetSuite REJECTS a duplicate.* Two identical creates carrying the same
`externalId`:

```
C1  {"success": true,  "recordId": "5264548", ...}
C2  {"success": false, "error": "HTTP 400 ... o:errorDetails:
     [{"detail":"Error while accessing a resource. This entity already exists.",
       "o:errorCode":"USER_ERROR"}]"}
C3  SELECT COUNT(*) ... WHERE externalid = '<key>'  →  1
```

This is **stronger** than a client-supplied header would be: the uniqueness is enforced
server-side by NetSuite, not trusted from us. A blind retry cannot create a second record
— it is refused — and the refusal is *distinguishable* (`This entity already exists`), so
a retry hitting that error means **the original landed**, which is precisely the fact the
`Unknown` state needs.

**Design consequences, all load-bearing:**

- The work-derived key goes in `data.externalId`, not a header.
- Reconcile an `attempted` row with
  `SELECT id FROM <type> WHERE externalid = '<key>'` — one query, definite answer.
- A retry is *safe by construction*. If the first write landed, NetSuite refuses the
  second; if it did not, the retry succeeds. Either way the end state is one record.
- Distinguish `This entity already exists` from other `USER_ERROR`s and treat it as
  **success-on-retry**, not failure. Match on `o:errorDetails[].detail`, reusing
  `write_repair_bound.extract_netsuite_error_details` rather than a new parser.

**RISK — `externalId` is not ours.** It is a real business field, and integrations
commonly own it (Celigo among them). Writing our hash into it could collide with a
tenant's own keying or overwrite meaning we do not understand. Mitigations, to decide
before implementing:

1. **Never overwrite** — if the payload already carries an `externalId`, use *that* as the
   idempotency key and do not substitute ours. The user's key is a better natural identity
   than our hash anyway.
2. **Namespace ours** — `ss-idem-<sha256[:24]>`, so a value we generated is identifiable
   on sight and cannot plausibly collide with a human-chosen key.
3. **Per-tenant opt-out**, if a tenant's integration requires `externalId` to stay empty.
   Then that tenant loses idempotency and batch must refuse to run for them — stated, not
   silently degraded.

*Probe residue:* sandbox customer `5264548` (`externalId=idem-probe-ab1b310b6b70`) was
created by this probe and can be inactivated.

### Phase 2 — Deterministic server-side extraction

The operator's standing decision, recorded 2026-08-28: model transcription is acceptable
for ONE record (the human reads every field on the card) and **not** for a batch, because
nobody eyeballs 200 rows. The model proposes a column→field mapping; the server reads the
values. Values never pass through the LLM on the batch path.

The mock's column-mapping strip is the visible surface of this.

### Phase 3 — The batch review surface

Only after 1 and 2. Per the mock:

- every row's values rendered, never a bare count
- environment badge as the loudest element on the page — enforcement still does not
  exist (`86bbp0pw3` shipped labelling only)
- bulk-fill for a missing required field with per-row override; this is what makes 200
  rows reviewable rather than theatre
- rows auto-excluded with a stated reason (empty required field, duplicate of an earlier
  row), never silently dropped
- commit bar naming the exact count and destination
- **Screen 2**: per-row outcome including `Unknown — sent, no answer`, and resume
  **blocked** until every unknown is resolved. A batch that silently skips its uncertain
  row is how a ledger acquires a duplicate nobody can explain.

### Phase 4 — Server-enforced cap

A ceiling on records per batch, enforced in code, not prompt text. `agent-graph.md` #4:
a cap the model is asked to respect is a request; a persisted counter is a guarantee.

## Invariants this must not break

- Every mutation still routes through `execute_tool_call` → `classify_mutation` →
  approval. A batch path that bypasses the dispatcher re-creates the HITL hole closed in
  PR #194 (`agent-graph.md` #3).
- No auto-post, ever. Approval covers *these rows*, not future rows.
- Per-record audit rows plus one summary, sharing a `correlation_id` — the pattern
  `reconciliation.approve_bucket` already uses. Note that precedent is a **DB status flip
  only**; it never posts to NetSuite, so it supplies the audit shape and *not* the safety
  model for N external writes.
- Irreversible steps last (`agent-graph.md` #9): validate every row, then write.

## Acceptance

1. `kill -9` during a batch leaves every row in a state that is true, and resume reaches
   a correct end state without creating a duplicate. Drilled, not reasoned about.
2. A row whose write times out is `Unknown`, never `written` and never `failed`.
3. Approving a batch writes exactly the approved rows — verified against NetSuite, not
   against our own status field.
4. The rendered surface is checked against the mock. Green tests are not the gate;
   `feedback_visualize_before_building` — acceptance is the rendered artifact.
5. A live run on sandbox `6738075-sb1`, with `origin/main` recorded before and after, and
   the result void if it moved (staging auto-deploys on every main merge).

## Explicitly out of scope

- Photos / scanned PDFs (vision path, Anthropic-adapter-gated)
- Cross-record-type batches — one record type per batch
- Undo. NetSuite writes are not transactional across records; the honest answer is
  *don't write it wrong*, which is what the review surface is for.
