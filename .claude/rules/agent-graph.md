---
description: Rules for unattended execution — anything a schedule, not a human, can start. Loads when editing workers, chat orchestration, MCP, or recon.
paths:
  - backend/app/workers/**
  - backend/app/services/chat/**
  - backend/app/mcp/**
  - backend/app/services/reconciliation/**
---

# Unattended execution

The test: *if this runs at 03:00 and nobody is watching, what happens?*

Everything here failed the recoverability test — it cannot be derived from reading the
code, so it has to live somewhere. Anything that **is** recoverable from the code was cut
from this file on 2026-08-03 and points at the code instead.

## Design decisions you must not re-litigate

1. **The mutation guard's deny-list is deliberate.** `_BLOCKED_RECORD_TYPES`
   (`chat/mutation_guard.py:31`) blocks system record types by name; `is_safe_record_type`
   returns `record_type not in _BLOCKED_RECORD_TYPES`. General security advice says prefer
   allow-lists — do not "fix" this one without a decision, because the tool surface it
   guards is open-ended and an allow-list there fails closed on every new record type.
2. **Recon results are keyed on `run_id`** (`models/reconciliation.py:43`), not on a
   business key. Cross-run carry-forward via a business-keyed disposition table is a
   *proposal*, not current law — see the program plan. Do not write code assuming it exists.
3. **Guard at the choke point, not the caller.** `execute_tool_call` (`chat/tools.py:247`)
   is the single dispatcher and has 7 callers; `classify_mutation` is called from only one
   of them. Adding a caller must not be able to add a hole. This is a known open gap, not
   the intended design.

## Loop rules — for anything a schedule starts

4. **Termination lives in run state, not in the prompt.** A cap the model is asked to
   respect is a request; a counter that persists and decrements is a guarantee.
5. **Terminate with a reason, not a boolean** — `done | budget | stall | error`, written to
   `jobs.result_summary`. "Stopped" merges "finished" with "stuck", and a human cannot
   route what they cannot distinguish.
6. **Bound cost, not just call count.** Read-only is not safe: a strictly read-only agent
   billed 8x its estimate on 20 queries because one was a full scan. Budget tokens, tool
   calls, API calls and query cost, checked at spend granularity.
7. **Verification is code, and never the same model in the same context.** Same context
   sees the same blind spot. In this repo the executable form is `scripts/verify.sh` — an
   AST or grep check over source cannot catch a name-resolution error.
8. **Anything that creates must delete in the same commit** — sessions, teams, worktrees,
   temp records. If nothing counts what is alive, it is already leaking.

## Irreversible external writes

Rules 10 and 12 are now BUILT for the chat write path (2026-09-01) — the rest is still
design intent for the posting engine, whose binding version lives in
`docs/superpowers/plans/2026-08-02-autonomous-accounting-ops-program.md`.

9. **Order irreversible steps last** — a failure before an irreversible step needs no
   compensation at all. Reordering is free; writing compensations is not.
10. **Every external write needs a work-derived idempotency key and a side-effect log
    written *before* the call**, so a crash between send and confirm is recoverable.
    *Built:* `chat/write_side_effect.py` (key) + `write_side_effect_repo.py` (log) +
    `write_side_effects`. The key goes in `externalId`, where **NetSuite enforces
    uniqueness server-side** — measured, not assumed: a repeated create returns HTTP 400
    "This entity already exists", so a blind retry cannot duplicate and hitting the
    refusal *proves* the original landed. Ordering is `record_attempt` → **commit** →
    call → `settle_from_result`; only a definite answer moves a row off `attempted`.
11. **An HMAC token proves payload integrity, not human freshness.** Any code holding the
    secret can mint one (`mutation_guard.py:100-134`); it is not evidence a person looked.
12. **Recovery code that has never run is not recovery code.** A `kill -9` mid-write drill
    is a required step of any posting PR's gate. *Run 2026-09-01*, both branches: child
    commits the attempt, dispatches, SIGKILLs itself (exit 137); a fresh process finds the
    unsettled row and settles it by ASKING NetSuite — `written` when it landed, `rejected`
    when it did not, zero unsettled remaining either way.
12a. **Reconciliation may only conclude from a key that was actually SENT.** "No record
    with this externalId" means the write never landed *only if* the payload carried that
    externalId. For an unstamped payload the same empty answer is meaningless, and reading
    it as "safe to retry" invites the duplicate the scheme exists to prevent. Enforced in
    `reconcile_by_external_id` by comparing the key against `payload_json` — the payload
    actually sent — and refusing to ask when they differ.

## Choosing a shape

13. **Pick the topology from the constraint.** Quality floor → generate-verify cycle with a
    mandatory exit. Latency ceiling → fan-out. Divergent input kinds → router. **None of
    those → a straight pipeline.** The simplest shape lives longest.
14. **Parallelism buys time, not money** — fan-out costs the same tokens as a pipeline, two
    branches are usually slower than serial, and widening improves the mean while worsening
    the worst case. Filter cheap (hash) before comparing expensive (model call).
15. **Framework or plain loop? Ask whether you need interrupt and resume.** No → a loop and
    a function call. Yes → durable execution. Our nightly Beat cadence is already layer
    scheduling; do not import an execution framework for a linear pipeline that is cheap to
    re-run.

---

*Cut from 35 rules to 15 on 2026-08-03 after an audit found rules contradicting shipped
code, generic security truisms with no repo noun, present-tense law for an unbuilt engine,
and a five-question footer duplicating rules within the same file. Alerting rules moved into
`services/ops_digest.py`, where the code they describe self-documents them.*
