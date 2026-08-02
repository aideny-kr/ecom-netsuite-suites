---
description: Agent-graph engineering doctrine — how we build scheduled jobs and agentic flows that touch money. Loads when editing workers, chat orchestration, or MCP.
paths:
  - backend/app/workers/**
  - backend/app/services/chat/**
  - backend/app/mcp/**
  - backend/app/services/reconciliation/**
---

# Agent-graph rules

Source: 『그래프 엔지니어링』(leaf meta, 2026-08-01), Agent-Graph track (ch18–26, 30–31).
Adopted 2026-08-02. Program plan: `docs/superpowers/plans/2026-08-02-autonomous-accounting-ops-program.md`.

These rules bind **unattended execution** — anything a schedule, not a human, can start.
The one-line test: *if this runs at 03:00 and nobody is watching, what happens?*

## 1. Guardrails are code on the edge, never prose

1. **A rule written in a prompt does not exist.** Context fills, the front gets summarized, the rule is gone — and the record that it was violated was in the summarized part too. Put invariants in a function on the call path. Conditions are code; code does not get summarized.
2. **Guard at the choke point, not the caller.** A check that lives in one caller is inherited by zero future callers. `execute_tool_call` (`chat/tools.py`) is the single dispatcher — a mutation check belongs there, not in each agent loop. Adding a caller must not be able to add a hole.
3. **Allow-list, never deny-list.** Deny-lists fail toward "should have blocked but passed" (you find out via incident); allow-lists fail toward "should have passed but blocked" (a user tells you in a minute). Deny-lists leak by construction — there are unlimited ways to spell a delete.
4. **Derive the allow-list from a registry so it cannot drift.** Reference implementation: `report/recipe.py:is_recipe_eligible()` — derives from the tool-category registry, *then* re-asserts `is_mutation_tool` even though that case is structurally unreachable. Copy this shape.
5. **Validate the interpreted value, not the string.** `realpath` before comparing paths; parse the URL and compare the host. String checks on `..` or `startswith("example.com")` are bypassable.
6. **Against prompt injection, permission is the only layer that works.** Delimiters and input scanning lower probability; removing the capability removes possibility. Natural language has no grammar separating instruction from data, so anything judged by content can be fooled by content. Scope the tool list — do not write a better warning.
7. **Least privilege is "no single node holds a fatal combination,"** not "each node has what its job needs." Count every path bytes leave by: network, files, logs, stdout, error messages, and the user-visible response. The response always goes out.

## 2. Unattended runs must terminate, and terminate legibly

8. **Max turns live in run state, not in the prompt.** Decrement per iteration and persist. A cap the model is asked to respect is not a cap.
9. **Verification is code, never the same model in the same context.** Same context sees the same blind spot. Use assertions, parsers, Decimal comparisons, `pytest`. A model-graded check is a suggestion.
10. **Termination returns a reason enum, not a boolean.** `done | budget | stall | error`. "Stopped" merges "finished" with "stuck," and a human cannot route what they cannot distinguish. Write it into `jobs.result_summary`.
11. **Budget cost, not just calls.** Read-only is not safe — a strictly read-only agent can bill unboundedly on scanned bytes. Bound tokens, tool calls, NetSuite API calls, and query cost, checked at spend granularity.
12. **Anything that creates must delete in the same commit.** Sessions, teams, worktrees, temp records. If nothing counts what is alive, it is already leaking.

## 3. Irreversible steps (the posting rules)

13. **Order irreversible steps last.** A failure before an irreversible step needs no compensation. Reordering the pipeline is free; writing compensations is not. `reconcile → draft → post → notify` — never notify before post.
14. **Every external write needs a work-derived idempotency key + a side-effect log.** Key = `sha256(period, account-combo, source-doc-hash, amount)` — derived from the work, not from the attempt. Log `started → posted(ns_internal_id) | failed` *before* the call, so a crash between send and confirm is recoverable.
15. **The crash window can be narrowed, never closed.** Write "started" first, stamp an external ref on the NetSuite record, and run a sweeper that resolves stale `started` rows by asking NetSuite for ground truth.
16. **Compensation is "must eventually succeed," not "must succeed now."** On failure it goes to a persistent compensation queue, not a retry loop and not a log. That queue is inside our DB, so it is transactional — the regress stops there.
17. **Recovery code that has never run is not recovery code.** A `kill -9` mid-post drill on staging is a required step of any posting PR's T2 gate.
18. **Retries need jitter, and recovery needs a circuit breaker with a one-request probe.** Backoff alone does not lower the peak — everyone still retries together. On recovery, send exactly one probe; a herd re-kills the server that just came back.
19. **Timeouts are a budget, not layers.** If the sum of inner timeouts exceeds the outer timeout, the config is a lie. Reserve later stages' minimums first, so the stage that gets truncated is never the one that protects quality.
20. **Classify failures before retrying.** 4xx (except 429) permanent → straight to a human; 429/5xx transient → backoff; unknown → a few more tries, then human. Do not try to eliminate the unknown bucket.

## 4. Approvals and human gates

21. **An approval binds to what was shown.** Persist a hash over only the decision-relevant fields (amount, deposit id, variance, bucket). The executor recomputes before acting and re-asks on mismatch. Hashing everything causes approval fatigue; hashing nothing means "approved $X" can post $X′.
22. **Pausing for a human is a suspend, not an exception.** Never re-run from step 1 to reach the approval point — the re-run can produce a different result than the one the human approved.
23. **An HMAC token proves payload integrity, not human freshness.** Any code holding the secret can mint one. It is not evidence a person looked.
24. **A stalled approval is invisible by design.** Queue age must be measured and escalated on a clock; "someone will notice" is how an item sits for two weeks.

## 5. Alerting

25. **Alert volume is inversely related to alerts read.** Per-failure alerts train people to ignore the channel. One daily digest (`failed jobs, connections in error, DLQ backlog, top-3 causes`) plus a hard page past a backlog threshold. Fewer alerts, more attention.
26. **A dead-letter entry must be re-runnable from the record alone.** Required fields: `job, payload, idempotency_key, reason, kind, tries, first_failed_at, trace_id`. If a human has to read code to replay it, it is a log, not a queue. Auto-escalate past 24h.
27. **Reading the DLQ is bug-list extraction, not backlog clearing.** Top 3 causes are usually most of the volume and often one code fix.

## 6. Choosing a shape (do not over-build)

28. **Pick the topology from the constraint, not from ambition.** Quality floor → generate-verify cycle (with a mandatory exit condition). Latency ceiling → fan-out/fan-in. Divergent input kinds → router. **None of the above → a straight pipeline.** The simplest shape lives longest.
29. **Parallelism buys time, not money.** Fan-out costs the same tokens as a pipeline. Two branches are usually *slower* than serial. Widening improves the mean and worsens the worst case — and users feel the worst case.
30. **Filter cheap before comparing expensive.** Hash/fingerprint first, model-call only the survivors. A join that calls a model pairwise is O(n²) and will dominate the run.
31. **Framework vs. plain loop: do you need interrupt and resume?** No → a loop and a function call. Yes → durable execution. Do not import an execution framework for a linear pipeline that is cheap to re-run; our nightly Beat cadence *is* layer scheduling.
32. **Do not reach for a graph database.** Tables win on constraints and transactions, which financial data needs most. Our matching is a 1-hop lookup on `order_ref`; measured graph advantage starts at ≥3 hops.

## 7. Facts that change over time

33. **Dispositions are facts about a business key, not rows about a run.** Key on `(tenant_id, order_ref)`, never `run_id`. Runs are observations; dispositions are facts.
34. **Correct by insert, never by UPDATE-in-place.** Carry `valid_from/valid_to`, close the open interval when writing a new value, read half-open (`vfrom <= t AND vto > t`). "What did we believe on close date" must stay answerable.
35. **History not captured now is blank forever.** This is the one thing that genuinely cannot be retrofitted.

## Five-question audit (run against any new flow)

1. Which node has the highest degree? That is the next bottleneck.
2. Is there a cycle with no exit condition? That is the next bill.
3. Is anything created without being deleted? If nothing counts them, it is already leaking.
4. Is the rule in the prompt or on the edge? In the prompt = gone the day context fills.
5. What does one edge carry in tokens? Not knowing means not controlling cost.
