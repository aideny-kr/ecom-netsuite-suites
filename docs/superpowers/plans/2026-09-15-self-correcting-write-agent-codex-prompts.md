# Codex prompts — a self-correcting NetSuite write agent

> Date: 2026-09-15 · For: OpenAI Codex (CLI or cloud), one prompt per session, in order.
> Goal: when a NetSuite write is rejected, the agent diagnoses the rejection, changes its approach, re-validates, and tries again — the way a coding agent fixes a failing edit — **without ever creating a duplicate record and without bypassing human approval.**

## Why this is not "just retry"

The coding-agent analogy holds for one class of failure and is dangerous for another.

| Failure class | Example | Correct behaviour |
|---|---|---|
| **Rejected payload** (HTTP 400 with `o:errorDetails`) | invalid field id, missing required field, bad reference, closed period, wrong currency shape | The payload was wrong. Diagnose, recompose *differently*, re-validate, resubmit. This is the Claude-Code-style loop. |
| **Transport / indeterminate** (timeout, 5xx, connection reset, empty body) | `ns_createRecord` exceeded 15 s but NetSuite created the record anyway (observed 2026-08-27, customer 5264348) | The payload may have been perfect. **Never recompose, never resubmit blind.** Reconcile by reading (external id / idempotency key), then either mark posted or retry the *identical* idempotent request. |
| **Auth / rate** (401, 429 `CONCURRENCY_LIMIT_EXCEEDED`) | expired token, shared concurrency pool exhausted | Refresh or back off and retry the same request. The model never sees these. |
| **Permission / policy** (403 `INSUFFICIENT_PERMISSION`, feature disabled) | role lacks the permission | Cannot be fixed by recomposing. Stop, explain, escalate. |
| **Business decision** | "period is closed", "insufficient inventory", "customer is inactive" | A human must choose. Delegate the specific field/decision via the existing `ask_user` slot; do not guess. |

The repo already has the skeleton: a pre-approval repair loop (`WriteRepairState`, `max_attempts=2`, stall detection by fingerprint), a post-approval bound (`write_repair_bound.decide_repair_bound`), a three-state outcome classifier (`write_outcome.classify_write_outcome`: success / failed / indeterminate), curated required fields (`required_field_registry.py`), server-verified `ask_user` slots (`slot_option_sources.py`), and a read-only recovery pass for unknown outcomes in transaction_ops (`recovery.py`). What is missing is the **middle**: an error taxonomy that routes each class to the right strategy, metadata-grounded repairs instead of "here is the error text, try again", a diversity rule that refuses a retry that changes nothing, approval-preserving resubmits for cosmetic fixes, and memory so the same fix is not rediscovered every time.

Build that in code at the choke point. Do not add rules files, prompts-as-policy, or new process docs.

---

## Preamble — paste at the top of every Codex session

```
You are working in the Suite Studio repository (FastAPI + SQLAlchemy async + Celery backend under backend/, Next.js frontend under frontend/). Read AGENTS.md if present, then CLAUDE.md, then .claude/rules/chat-orchestration.md and .claude/rules/agent-graph.md before touching code.

Non-negotiables for this work:
- TDD: write the failing test first, then the implementation. Run `cd backend && python -m pytest tests/ -q -k "write or repair or outcome"` before every commit and the full suite before the final commit.
- Every NetSuite write is human-approved (HITL). Nothing you build may execute a write the human did not approve, and nothing may weaken `mutation_guard.py`, `write_confirmation_service.py`'s HMAC token, or the `execute_tool_call(..., human_approved=False)` default in `services/chat/tools.py`.
- A write whose outcome is indeterminate (timeout, 5xx, unparseable response) is NEVER resubmitted until a read has established whether the record exists. `write_outcome.classify_write_outcome` is the source of truth for that state; do not sniff error strings.
- Bounded: every loop has a persisted counter and a termination reason (`done | budget | stall | error | escalated`), never a prompt-level cap.
- Structural, never textual: classify errors from `o:errorDetails[].o:errorCode` / HTTP status / structural markers, never by regex over messages.
- One commit per logical change, never amend, trailer `Co-Authored-By: Codex <noreply@openai.com>`. This is a T2 change (money, HITL, credentials): expect a blocking multi-angle review before merge.
- Use `python scripts/codegraph.py def|callers|uses <symbol>` to find definitions and callers instead of grep.
- Do not add new rules files, CLAUDE.md sections, or process documents. Put explanations in docstrings next to the code.
- Duplicate prevention is a property of ONE side-effect log with a uniqueness constraint on a business-derived work key, never of a single agent's memory or of a prompt. Two chat sessions, a chat and a scheduled job, or two backfill workers must all hit the same claim. Any new write path claims in that log before the external call or it does not ship.

Files you will touch or read (backend/app/services/chat/ unless stated):
  agents/base_agent.py (repair loop ~L1905-2050; WriteRepairState L38-110), write_repair_bound.py, write_outcome.py, write_validation.py, write_validator.py, required_field_registry.py, slot_option_sources.py, write_confirmation_service.py, mutation_guard.py, tools.py (execute_tool_call), ../mcp_client_service.py (timeout → INDETERMINATE_KEY), ../transaction_ops/netsuite_transport.py, ../transaction_ops/recovery.py, ../transaction_ops/executor.py.
Tests that already exist and must stay green: backend/tests/test_write_repair_bound.py, test_write_outcome.py, test_ask_user_and_repair_chain.py, test_write_confirm_orchestrator.py, test_write_validation*.py, test_forced_write_proposal.py, test_governance_result_outcomes.py.
```

---

## Prompt 1 — Error taxonomy and repair-strategy registry (the core)

```
Build the error taxonomy and strategy registry that every NetSuite write failure routes through.

READ FIRST: write_repair_bound.py (how o:errorDetails is extracted and fingerprinted), write_outcome.py (the three outcomes), base_agent.py L1905-2050 (where a rejected write re-enters the model today), mcp_client_service.py L280-365 (where timeouts are marked INDETERMINATE_KEY), transaction_ops/netsuite_transport.py (the typed NetSuiteActionError codes — reuse the naming style).

BUILD backend/app/services/chat/write_failure.py:
1. `classify_write_failure(result: Any, http_status: int | None) -> WriteFailure` where WriteFailure is a frozen dataclass: `klass` (enum below), `error_code: str | None` (from o:errorDetails[].o:errorCode when present), `details: list[str]`, `field_hints: list[str]` (field ids named in the details, extracted structurally from NetSuite's `o:errorDetails[].o:errorPath` / `o:errorQueryParam` when present — never regex over the message), `retryable_same_payload: bool`, `fingerprint: str`.
2. Classes: `transport_indeterminate` (INDETERMINATE_KEY set, or unparseable, or 5xx, or empty body) · `auth_expired` (401 / INVALID_LOGIN_ATTEMPT / token expired) · `rate_limited` (429 / CONCURRENCY_LIMIT_EXCEEDED / SSS_REQUEST_LIMIT_EXCEEDED) · `permission` (403 / INSUFFICIENT_PERMISSION / feature not enabled) · `invalid_field` (unknown/invalid field id, wrong type, INVALID_FLD_VALUE without a reference) · `invalid_reference` (a select/reference value that does not exist or is inactive: RCRD_DSNT_EXIST, INVALID_KEY_OR_REF, INVALID_RCRD_REF) · `missing_required` (SSS_MISSING_REQD_ARGUMENT, USER_ERROR "Please enter value(s) for") · `business_rule` (closed/locked period, inactive entity, insufficient inventory, over credit limit, duplicate external id — anything a human must decide) · `unknown`.
   The mapping table lives in ONE place with a docstring per row citing the NetSuite error code it matches. Use `o:errorCode` first, HTTP status second, and the structural INDETERMINATE marker for transport; `unknown` is the fall-through, never a guess.
3. `strategy_for(failure: WriteFailure) -> RepairStrategy` where RepairStrategy is one of: `reconcile_then_maybe_retry` (transport_indeterminate), `refresh_auth_retry_same` (auth_expired, once), `backoff_retry_same` (rate_limited; honour Retry-After, otherwise exponential with jitter, max 3, and respect the account's concurrency tier — see plan §3.7: Standard 5 slots), `stop_escalate` (permission, unknown after one attempt), `recompose_with_metadata` (invalid_field, invalid_reference, missing_required — Prompt 2 supplies the grounding), `delegate_to_human` (business_rule → ask_user slot or a proposal card with the decision spelled out).
4. Wire it: in base_agent.py, replace the current "rejected → feed error text back to the model" branch with a call to `classify_write_failure` + `strategy_for`. Transport/auth/rate classes are handled by code and NEVER reach the model. Only `recompose_with_metadata` and `delegate_to_human` produce a model turn, and the model receives the structured WriteFailure (class, code, field_hints) rather than raw text. Keep `write_repair_bound.decide_repair_bound` as the post-approval budget; make the budget per class (transport: 1 reconcile + 1 identical retry; rate: 3; recompose: 3; business: 0 — it goes to a human).
5. Termination: every path ends with a reason written to the run state / job row: `done | budget | stall | error | escalated`, and the reason names the class (`escalated:permission`).

TESTS (backend/tests/test_write_failure.py, write them first):
- One test per error class with a realistic NetSuite RFC-9110 problem document (nest o:errorDetails at two different depths; the fingerprint must be identical either way).
- A timeout result with INDETERMINATE_KEY classifies as transport_indeterminate even though it also carries an `error` string (this is the regression that created a duplicate customer).
- A 401 and a 429 never produce a model turn (assert the recompose callable is not invoked).
- Budgets are per class and persisted; a 4th rate-limited retry returns `budget`.
- `unknown` after one recompose attempt returns `escalated:unknown`, not another attempt.

DONE WHEN: all new tests pass, the seven existing write tests still pass, and `python scripts/codegraph.py callers classify_write_failure` shows base_agent.py as the single dispatch site.
```

---

## Prompt 2 — Metadata-grounded recomposition and the diversity rule

```
Make `recompose_with_metadata` actually diagnose before it retries, and refuse retries that change nothing.

READ FIRST: required_field_registry.py (curated required fields, why runtime discovery was rejected), slot_option_sources.py (`resolve_ask_user_slots`, server-verified option fetch), write_validation.py (`resolve_curated_metadata`, `validate_mutation`), record_metadata_service.py, reference_field_labels.py, netsuite_metadata_service.py (custom field/record discovery), and the `ask_user` handling in base_agent.py L1936-2050.

BUILD backend/app/services/chat/write_repair_grounding.py:
1. `ground(failure: WriteFailure, record_type: str, payload: dict, ctx) -> Grounding` that, per class, gathers facts BEFORE the model recomposes:
   - invalid_field: look the offending field id up in the record's metadata (`ns_getRecordTypeMetadata` via the existing metadata service, cached); return the closest valid ids (exact case-insensitive match first, then custom-field ids with the same suffix), the field's type, and whether it is a sublist column. If the field simply does not exist on this record type, say so — the fix is to drop it, not rename it.
   - invalid_reference: resolve the reference the same way `resolve_ask_user_slots` does (real options from the account); return the top candidates by label similarity plus their internal ids and `isinactive`. If exactly one candidate matches, propose it; if several, this becomes `delegate_to_human` with those options as the slot choices.
   - missing_required: consult `required_field_registry` for the record type; if the field has an option source, fetch options; if the model cannot know the value, route to `ask_user` (the existing delegation) rather than inventing one.
   - business_rule: no grounding; produce the human-readable decision ("Period Jul 2026 is closed. Post to the current open period Aug 2026, or stop?") with the concrete alternatives as slot choices.
   - exemplar (every class except transport / auth / permission): read ONE recent record of the same type from this account (SuiteQL for the latest id, then `ns_getRecord`), redact amounts and PII, and hand the model its SHAPE — which fields are populated, their formats (date, currency, reference-by-id), which custom fields carry values, which sublists exist. This is the move a coding agent makes when it reads a neighbouring file before editing: the customer's account, not the docs, is the ground truth for what a valid record looks like here. Fetch it at most once per proposal and cache it on the repair state.
   - verbatim: the Grounding always carries the full, untouched `o:errorDetails` (code, message, errorPath) next to the classification. Never summarise or condense the error on its way to the model; a coding agent self-corrects because it sees the whole compiler output, not a one-line paraphrase.
2. Diversity rule (pre-emptive stall): before a recomposed payload is submitted, compute the `write_repair_bound` fingerprint of the FIELDS NAMED IN `failure.field_hints` (or the whole payload when no hints). If the recomposed payload is unchanged at those fields, refuse the attempt with reason `stall` immediately — do not spend a NetSuite call to learn what you already know. Keep the existing post-hoc stall check as the backstop.
3. The model's recompose prompt is built from the Grounding object (structured), and the instruction is explicit: "Change only what the grounding indicates. Do not touch fields the human approved unless the grounding names them." Log the diff between attempts.
4. Local re-validation: every recomposed payload goes through `validate_mutation` again before it can be submitted; a payload that fails local validation never reaches NetSuite. Where a cheap, side-effect-free probe exists, run it too before the live call — a SuiteQL existence check for every reference id in the payload, and an open-period check for the transaction date — so the first live attempt is already the second attempt. (A RESTlet "dry build" — `record.create` in dynamic mode with `setValue` and no `save()` — catches invalid select values and some field-level rules but NOT mandatory-field or save-time workflow errors; use it only as an extra probe, never as proof.)

TESTS (backend/tests/test_write_repair_grounding.py, first):
- invalid_field with a case-mismatched id (`custbody_Order_Total` vs `custbody_order_total`) grounds to the exact id and the recomposed payload differs only there.
- invalid_field for a field absent from the record type grounds to "drop", and a recomposition that keeps the field is refused as `stall`.
- invalid_reference with one candidate proposes it; with three candidates produces a delegate_to_human with those three options.
- missing_required for `customer.subsidiary` (the one account-evidenced entry in the registry) routes to ask_user with real options from a stubbed metadata call.
- Diversity rule: identical payload at the hinted fields → `stall` with zero transport calls (assert the fake transport was not called).
- Exemplar is fetched once per proposal (second failure on the same proposal reuses the cached shape; assert one metadata read) and the recompose prompt contains its field-shape summary and the verbatim errorDetails.
- Pre-call probes: a payload referencing a non-existent customer id is refused locally with reason `error:reference_absent` and zero write calls.
- Every test uses fakes for metadata and the LLM; no network.

DONE WHEN: a scripted sandbox drill (`scripts/uat/`, opt-in, real sandbox creds from env) shows a deliberately wrong custom-field id being corrected on the second attempt and a deliberately wrong subsidiary being delegated to the human, with the run's reason trail recorded.
```

---

## Prompt 3 — Approval-preserving resubmit: semantic payload vs wire payload

```
Let cosmetic repairs resubmit without a new approval card, and make every semantic change re-approve, by binding approval to what the human saw rather than to the bytes on the wire.

READ FIRST: write_confirmation_service.py (`build_confirmation_payload`, `mint_confirmation_token`, `validate_and_extract_confirmation`, `merge_slot_values`), mutation_guard.py (HMAC), write_payload.py, the approve branch in orchestrator.py that passes `human_approved=True`, and the "HMAC proves payload integrity, not human freshness" note in .claude/rules/agent-graph.md #11.

BUILD:
1. Define the SEMANTIC payload = the (record_type, field → value) mapping the card displayed, with reference fields expressed as (label, internal id) and amounts as Decimal strings. Define the WIRE payload = the JSON actually sent. The HMAC token binds the semantic payload.
2. `classify_repair_delta(before_semantic, after_semantic) -> "cosmetic" | "semantic"`. Cosmetic = the semantic mapping is byte-identical (only wire encoding changed: field id casing, date format, reference expressed by id instead of label, sublist shape). Semantic = any value, any added or dropped field, any changed reference.
3. Auto-resubmit envelope, in code: a repair may resubmit without a new card only when (a) the delta is cosmetic, (b) the failure class was invalid_field or missing_required-with-derivable-default, (c) the per-class budget allows it, (d) the token is still valid. Everything else opens a NEW confirmation card whose diff view highlights exactly which fields changed and why (the Grounding), and the old token is invalidated.
4. Never let a resubmit change the target account or environment (the 2026-08-27 sandbox-binding decision: environment is derived server-side from the account id and signed into the envelope).
5. Audit: every resubmit writes an audit event with correlation id, attempt number, class, delta kind, and the field diff.

TESTS (backend/tests/test_write_resubmit_envelope.py, first):
- A field-id casing fix is cosmetic → resubmitted with the same token, one audit row, no new card.
- Changing an amount or a subsidiary is semantic → new card, old token rejected by `validate_and_extract_confirmation`.
- A cosmetic delta on a `business_rule` failure still opens a card (class gate).
- A repaired payload pointing at a different account id is refused regardless of delta.
- Token replay after invalidation is refused.

DONE WHEN: the confirmation card's diff view (frontend `write-confirmation-card.tsx`) renders the repair diff, and a vitest covers the "repaired — 2 fields changed" state. Mock the card state first per report-design.md if you add any new visual state.
```

---

## Prompt 4 — Learned repairs: remember the fix

```
Persist successful repairs so the same rejection is fixed on the first attempt next time, without turning memory into an unreviewed write path.

READ FIRST: models/tenant_learned_rule.py and learned_rule_service.py (free-text rules injected into the prompt — this is NOT that), write_validation.py (`normalize_for_validation` is the natural place for a pre-validation transform), the audit model, and the reject-label work in reconciliation (`recon_reject`) for the labelling pattern.

BUILD:
1. Migration + model `write_repair_rules` (tenant-scoped, RLS like every other table): `record_type`, `failure_fingerprint` (from write_repair_bound), `failure_class`, `field_path`, `transform` (a small, closed set: `rename_field(from,to)`, `drop_field`, `coerce_date_format(fmt)`, `reference_by_id`, `default_value(value)` — no arbitrary code), `approvals` (count of human-approved writes that used it), `last_used_at`, `created_from_correlation_id`.
2. Capture: when a repair attempt succeeds (NetSuite accepts and the human approved the semantic payload), and the delta was one of the closed transforms, upsert a rule keyed on (record_type, failure_fingerprint, field_path).
3. Apply: in `normalize_for_validation`, apply matching rules as a pre-validation transform and record which rules fired on the card ("applied 2 learned fixes: renamed custbody_Order_Total → custbody_order_total"). Rules never bypass validation or approval; they only make the first proposal right.
4. Hygiene: a rule that later produces a rejection is disabled automatically and surfaced; rules are listed and deletable on the Settings AI tab (reuse the learned-rules section pattern). Cap rules per tenant; oldest-unused evicted.
5. Metrics: first-attempt success rate for writes, repair attempts per write, rules fired per write — emitted to the existing metrics module so the effect is measurable on Framework within a month.

TESTS (backend/tests/test_write_repair_rules.py, first):
- A successful rename repair creates a rule; the next proposal for the same record type arrives at NetSuite with the corrected id on attempt 1 (fake transport asserts exactly one call).
- A rule whose transform is not in the closed set is rejected at write time (schema-level).
- A rule that fires and then the write is rejected is disabled and an audit event names it.
- RLS: a rule from tenant A never applies to tenant B (use the existing two-tenant fixture pattern).

DONE WHEN: migration up/down round-trips in CI's migration check, the seeded-tenant e2e exercises capture → apply, and the metrics appear in the metrics catalogue.
```

---

## Prompt 5 — Transport-class handling and reconcile-before-retry in the chat write path

```
Bring the transaction_ops recovery discipline to the general chat write path so an indeterminate outcome is reconciled by reading, never by resubmitting blind.

READ FIRST: mcp_client_service.py L280-365 (INDETERMINATE_KEY), write_outcome.py, transaction_ops/recovery.py + netsuite_transport.py (`read_created_snapshot`, the live re-read guard), transaction_ops/executor.py (`verify_outcome`), and plan §3.7 for the concurrency tiers.

BUILD:
1. Idempotency on every create, derived from the BUSINESS IDENTITY of the record, never from the proposal or the session: `work_key = business_digest({account, subsidiary, record_type, natural_key})` where `natural_key` is the record's own identity (order reference / tranid / source system id / customer email+subsidiary; for records with no natural key, the approved semantic payload's stable hash). Copy the shape from `transaction_ops/state_service.claim_approved_operation` (its `entity_key`). Set `externalId` (or a documented custom body field when the record type lacks one) to `ss:{tenant}:{record_type}:{work_key}` BEFORE the call. A proposal-scoped key would let two agents that each propose the same customer create it twice — that is the case this exists to prevent.
1b. Claim before call: INSERT a side-effect log row (`started`, with work_key, proposal_id, correlation_id, wire payload hash) under a UNIQUE (tenant_id, work_key) constraint, commit, then make the external call on a connection returned to the pool — claim-then-release, never `FOR UPDATE` across network I/O (see the orchestrator's `_cas_claim_write_confirmation` comment on Supabase's statement timeout). A unique-violation on insert means another writer already holds or finished this work: stop with reason `duplicate:in_flight` or `duplicate:posted`, and show the human the existing row (its status, internal id, who approved it). This is agent-graph.md #10 and the posting-ladder's `netsuite_posting_log`. If V-02 (durable execution) has already added the log, reuse it; otherwise add the minimal table now with a docstring pointing at V-02.
2. `reconcile_indeterminate(ctx, proposal) -> "posted" | "absent" | "unknown"`: SuiteQL/`ns_getRecord` by externalId (or by the guard's snapshot for updates), bounded to MAX_GUARD_READ_CALLS, with the same read-only discipline as recovery.py. `posted` → mark success, stamp the internal id, no resubmit. `absent` → the identical wire payload may be retried once (same externalId, so a race cannot double-post). `unknown` → stop with reason `error:reconcile_unknown` and a card that tells the operator exactly what to check.
3. `auth_expired`: call the existing proactive token refresh path once, retry the same request once, then `stop_escalate`.
4. `rate_limited`: respect Retry-After; otherwise exponential backoff with jitter (1 s, 2 s, 4 s), max 3; read the account's detected service tier (Standard 5 concurrent) from connection metadata if present and log it; never widen concurrency to "fix" a 429.
5. Every branch above runs without a model turn and writes its reason.

TESTS (backend/tests/test_write_transport_recovery.py, first):
- Timeout → reconcile finds the record by externalId → outcome success, zero resubmits, internal id stamped.
- Timeout → reconcile finds nothing → exactly one identical retry with the same externalId.
- Timeout → reconcile read itself times out → `error:reconcile_unknown`, no retry, card produced.
- 401 → refresh called once → retry once → success; a second 401 → `escalated:auth`.
- 429 with Retry-After: 2 → sleep is 2 s (fake clock) → retry; four 429s → `budget`.
- Two different approved cards for the same business identity (two sessions, two users) dispatched concurrently → exactly one external call; the loser stops with `duplicate:in_flight` and its card points at the winner's row. Same work_key from a fresh proposal after the first posted → `duplicate:posted`, zero calls.

DONE WHEN: the existing crash drill `scripts/uat/transaction_ops_crash_drill.py` pattern is mirrored for the chat write path (kill the worker after the stub save; on restart exactly one record exists and the proposal shows `posted`), and the drill is documented as a required T2 gate step in the PR description.
```

---

## Prompt 6 — Certification on a real sandbox (human-run, after 1–5 merge)

```
Write the certification script and run it against the sandbox account named in the deployment's connection (never production).

Scenarios, each must leave the reason trail described:
1. Wrong custom-field id → corrected on attempt 2 → posted. Reason: done; rules table gained one rename rule.
2. Same scenario a second time → correct on attempt 1 (learned rule fired). 
3. Subsidiary missing → ask_user card with real options → human picks → posted.
4. Post to a closed period → delegate_to_human card offering the current open period → human chooses → posted to the open period, never to the closed one.
5. Forced timeout (set the tool ceiling to 1 s for the run) → reconcile finds the record → posted, exactly one record in the sandbox.
6. Role without the permission → `escalated:permission` with a message naming the missing permission; zero retries.
7. Five parallel writes on a Standard-tier sandbox (5 slots) → some 429s → all complete within budget, none duplicated.
8. Two agents (two chat sessions, or one chat session and one scheduled job) approve a create for the same customer within the same second → exactly one record in the sandbox; the second shows `duplicate:in_flight` or `duplicate:posted` with the winner's internal id. Then force the same externalId through the raw MCP tool a third time and record NetSuite's exact error code for an existing externalId — that code becomes the server-side backstop mapped to `posted` in Prompt 1's taxonomy.

Record the correlation ids, attempt counts and reasons in the PR; inactivate every test record afterwards (STATE.md lists three leftover test customers in production from an earlier proof — do not repeat that).
```

---

## Prompt 7 — Multi-writer safety: chat, scheduled jobs and backfill workers share one claim

```
Make duplicate prevention a property of the system, not of any one agent. Today the chat path prevents the SAME card from being approved twice (`_cas_claim_write_confirmation`), and transaction_ops prevents two proposals for the same order (`claim_approved_operation`: tenant row lock, UNIQUE (tenant_id, work_key), in-flight check on account+subsidiary+record_type+order_reference). Nothing prevents two DIFFERENT cards in two sessions, a chat write racing a scheduled job, or two Celery workers in a backfill from posting the same business record. Close that at one choke point.

READ FIRST: transaction_ops/state_service.py L795-860 (`claim_approved_operation`), transaction_ops/executor.py (module docstring: "A duplicate delivery reads durable status. It never obtains another send permit."), orchestrator.py ~L2190-2240 (`_cas_claim_write_confirmation` and the comment on why the claim is not held across I/O), core/redis_lock.py, the posting log from Prompt 5 / V-02, and the V-03 backfill ticket (tier-aware concurrency: Standard 5, Premium 15, Enterprise/Ultimate 20 shared by REST, RESTlets, SOAP and the AI Connector).

BUILD:
1. One claim function, `posting_log.claim(db, tenant_id, work_key, *, surface, proposal_id, wire_hash) -> Claim | Existing`, used by all three surfaces (chat approve path, transaction_ops executor, backfill worker). It is the ONLY code allowed to insert a `started` row. Chat and transaction_ops call it from their existing approve/execute paths; the backfill calls it per item. Grep for every `ns_createRecord` / `ns_updateRecord` / RESTlet POST dispatch and route it through `claim` or explain in a docstring why a read-only path is exempt.
2. Backfill lease: work items live in a table with `UNIQUE (tenant_id, job_id, item_key)`; a worker takes items with `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n` in a short transaction, stamps `leased_by`/`lease_expires_at`, commits, then works. A sweeper returns expired leases to the queue; a returned item is reconciled by externalId (Prompt 5's `reconcile_indeterminate`) before it may be sent again. Never a process-local `asyncio.Semaphore` as the only limiter — with N workers that is N×tier.
3. Per-account concurrency permit in Redis: a counting semaphore keyed `ns_conc:{account_id}` sized from the connection's detected tier (default 5) with a reserve for interactive chat (backfill lanes = tier − 2, never below 1), acquired around the external call with a TTL so a dead worker's permit expires. 429 does not widen it; it triggers Prompt 5's backoff and a metric.
4. Server-side backstop: every create carries the externalId from Prompt 5. When NetSuite rejects a create because that externalId already exists, classify it as `duplicate:posted`, read the record, stamp the internal id, and never recompose. (Record the exact `o:errorCode` observed in Prompt 6 scenario 8; do not guess it.)
5. Stale `started` sweeper: a row `started` for longer than the tool ceiling + grace is reconciled by read; `posted` / `absent` / `unknown` written with a reason; `unknown` rows surface on an operator list, never auto-retried.

TESTS (backend/tests/test_posting_log_claim.py and test_backfill_lease.py, first):
- Two coroutines claim the same work_key concurrently → one Claim, one Existing; exactly one external call.
- Chat approve and a scheduled job with the same work_key → the second surface gets Existing with the first surface's status and proposal id.
- Two worker processes (subprocess, real Postgres) lease from one job → disjoint item sets; kill one mid-lease → its items return after expiry, each reconciled by externalId before resend, zero duplicates.
- Semaphore sized 5 with reserve 2 → at most 3 concurrent backfill calls (fake clock, fake transport); a dead holder's permit expires.
- externalId-exists rejection → `duplicate:posted`, one read, zero resubmits.

DONE WHEN: the crash drill from Prompt 5 is extended to two writers (two processes approving the same entity while the worker is killed mid-write) and shows exactly one record and one `posted` row; and `python scripts/codegraph.py callers ns_createRecord` (and update/RESTlet equivalents) shows every dispatch site behind `claim`.
```

---

## Sequencing and cost

Prompts 1, 5 and 7 are the safety floor and should land first, in that order: 1 gives every failure a class, 5 removes the duplicate-on-timeout and model-sees-429 paths for one writer, 7 removes duplicates across writers (two sessions, chat vs job, backfill workers) and is what makes the V-03 backfill sellable. Prompt 2 is where the "tries a different way" behaviour actually appears. Prompt 3 is what keeps HITL honest once repairs resubmit. Prompt 4 is the compounding win. Each is one PR, T2, reviewed before merge; together roughly three to four weeks for one engineer with Codex.

What this deliberately does not add: new rules files, prompt-level policy, a bigger repair budget without a class, or any path that writes to NetSuite without the human's approval of the semantic payload.
