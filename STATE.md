# STATE

## GOAL — read this before anything else

**Product.** Completely automate the end-to-end daily and monthly accounting operations
routine — reconciliation, close, reporting — via scheduled jobs and agentic flows, with
memory + reporting tools and **read AND WRITE** access to NetSuite and further MCPs.

*Where we are: Rung 1 of 4. The read half is real and well-gated. The write half does not
exist — zero posting code, no compensation, no durable execution. The longest-lead blocker
is evidence, not code: nothing today can produce the error rate unattended posting rests
on, because no reject/dispute action exists to generate negative labels.*

**How we work** (this is a means, not the goal). A development cycle that suits a frontier
model: minimum harness, loop engineering for feedback, graph engineering for flow, and one
place for state. Portable across projects — process global, knowledge local.

> The goal is the product. If a session is producing process artifacts and no movement on
> the ladder, that is drift — it happened on 2026-08-02 and cost a day.

---

The development loop's state: what the next session must know **without replaying history**.

Both of us read and write this. It lives in the repo because it has to survive a context
reset, a new session, and a different machine. ClickUp keeps the session ticket; work
state lives here — two systems only break at the seam between them.

Updated at the end of every task, not "later".

---

## NOW — in flight

| branch | tier | state | blocked on |
|---|---|---|---|
| `feat/dev-loop-and-harness` | T2 | 21 commits, process only — gated ×3, blockers fixed, **frozen** | nothing |
| `feat/agent-graph-operating-model` | T2 | Track O (22 majors) + reject action, **ungated** | needs Track O decision |
| `feat/rolling-period` | T2 | Stage 1 done. **PR #209 open, NOT merged.** verify **PASS @ `7f494264`**. Gate ×4 (budget spent; all pinned+codex): majors 2→3→0. Round-4 fixes landed | human merge call — deploy agents active. Then **Stage 2: scheduled compose** |

**SHIPPED 2026-08-17 — `fix/ns-account-switch-and-chat-burst` → PR #194, squashed to
`54729804`, deployed and live-verified on staging.** Ticket 86bba299w closed. It had
been invisible in this table for six days and read as "stalled, 2 days idle" when it
was one gate round from landing — that omission IS the failure mode this file exists to
prevent, which is why the entry went in before anything else.

Delivered: the OAuth silent account repoint (overwrite, but loudly), the chat burst cap
on all three entry points, the replica-safe MCP limiter — plus two blocker-grade
security defects the gate surfaced, both pre-existing and both AMPLIFIED by this
branch's own supersede logic, so they were pulled in rather than deferred: an
unauthenticated reflected XSS on the API origin, and a cross-tenant takeover via the
OAuth state's positional parse.

What to carry forward — the process lessons cost more than the code did:

- **Nine gate rounds, and rounds 5→8 were mostly fixing the previous round's fixes.**
  Three fixes were half-applied (callback template, escaping funnel, state validator):
  each correct, each applied to one of N call sites. The two that stuck removed the
  possibility instead — `render_callback()` (one template, no site can forget) and
  `encode_state()` (JSON, so no field can shift another). Ask "what makes this
  unrepresentable?" before "where else does this appear?".
- **~30 KB is the gate's practical ceiling.** 81.9 KB had to be split into path-chunks,
  proven lossless (every changed file in exactly one chunk, byte-identical). The cost
  is that no single run sees a cross-chunk seam.
- **`args` passed to `Workflow` arrive JSON-STRINGIFIED even when written as a proper
  object**, so `args.target` reads `undefined` and the gate silently reviews the
  CURRENT checkout — status still `OK`, findings plausible, all about the wrong branch.
  Drive it from a wrapper script and ALWAYS check the returned `target`/`base` first.
  Cost 4.6M tokens on an unrelated branch before the check caught it.
- **`failed_angles` counts FINDER angles only.** The last round lost all 163 verifier
  agents to the weekly rate limit and still reported `failed_angles: []` — a clean-
  looking pass with an entirely unadjudicated verify stage. Fail closed on the
  UNVERIFIED count too. **~161 of that round's findings remain unadjudicated; two were
  hand-checked and both were real blockers, so this is a genuine gap, not noise.** A
  fresh round on the merged diff is owed once quota resets.

Live-measured after deploy (30d of `audit_events`, staging): peak MCP tool rate is
9/min (`netsuite.suiteql`) against a 60/min ceiling — 6.7× headroom, and nothing else
is within 6× either. **`recon.approve_group` has ZERO calls in 30 days**, so its
40→20/min halving is untested by real usage; a close-week bulk-approval burst is the
one thing that data cannot see. Watch `mcp_rate_limit_rejections_total`.

Contents of that branch, honestly:

**Split 2026-08-04** — the two were on one 136 KB branch, which the review gate cannot
process in a single run (~30 KB practical limit).

**"Zero shared files" was FALSE and it is a merge hazard.** Verified 2026-08-05:
`comm -12` over the two name-only diffs returns **8 shared files** — the split copied the
process work onto `dev-loop` but never removed it from `agent-graph`, which therefore
still carries the PRE-FIX `scripts/ship.sh` (7 `TIER` references — the unbound-variable
blocker), a 240-line `scripts/verify.sh`, and `scripts/loop.sh`. Merging `agent-graph`
after `dev-loop` would silently resurrect both blockers. **Do not merge `agent-graph` as
it stands.** The reject slice is separable and clean: commits `8c8ca1f` + `01a42bd`, four
files (`recon_reject.py`, `093_recon_reject_labels.py`, `models/reconciliation.py`,
`test_recon_reject.py`), zero overlap with the process files — cherry-pick those onto a
fresh branch off `main` and leave Track O behind pending its own decision.

- **`feat/dev-loop-and-harness` (this branch, 47 KB)** — `verify.sh` · `ship.sh` ·
  `STATE.md`. **There is no `loop.sh`** — it was deleted on this branch (see DECIDED:
  enforcement lives in hooks); the two hooks in `~/.claude/hooks/` replace it and are
  the only part that runs whether or not the agent cooperates. Routine + verification
  standard now global in
  `~/.claude/CLAUDE.md`; gate target-check in `.claude/rules/uat-review.md`;
  `agent-graph.md` cut 35 rules → 15. Finished; gating now.
- **`feat/agent-graph-operating-model` (58 KB + 24 KB)** — Track O (ops digest, Sentry in
  workers, InstrumentedTask coverage; **22 open majors**) and the reject action (service +
  migration 093 + 16 tests, but **no endpoint, so no labels accrue yet**).
- **Track O — product code (PARKED)** — ops digest + Sentry-in-workers + InstrumentedTask
  coverage. Passed no gate round cleanly: **22 open majors** from round 2.

## NEXT — ordered, with the why

1. ~~Cut `agent-graph.md`~~ **DONE** — 35 rules → 15; the two that contradicted shipped
   code are now recorded as decisions not to re-litigate.
2. ~~Cut the ceremonial layer~~ **DONE** — both false lines fixed; routine + verification
   standard moved to `~/.claude/CLAUDE.md` (global, applies to every project), removing
   3.2 KB of duplication from this repo's always-loaded context.
3. ~~Convert the two rules I keep breaking into hooks~~ **DONE 2026-08-05** — both live in
   `~/.claude/hooks/` and merged into `~/.claude/settings.json` alongside the 9 existing.
   `record-verify-run.sh` was rewritten the same day: v1 fired on any command merely
   *containing* the string `verify.sh` and stamped the HOOK cwd's branch, so it logged
   phantom runs against a branch that was not under test. It now reads branch, sha and
   verdict out of verify.sh's own banner — the one source that only a real run emits.
4. ~~Single-agent baseline~~ **DONE 2026-08-05** — see DECIDED. One agent, 31× cheaper,
   both blockers, ~44% of distinct defects. Ordering changed; gate not retired.
5. ~~Build the dev-cycle graph~~ **DONE** — `verify.sh` (evidence) and `ship.sh` (gate
   target pinned). No `loop.sh`: a script you must choose to run enforces nothing, so
   the stopping rule moved to hooks. FRAME and SCOPE stay human nodes on purpose —
   that is ownership, not capability.
6. ~~Split this branch~~ **DONE 2026-08-04** — process extracted to
   `feat/dev-loop-and-harness`, zero shared files with the sibling branch.
7. **→ NOW: ship the reject endpoint + MCP tool.** The service, migration 093 and 16
   passing tests exist, but nothing exposes them — **no labels can be recorded, so the
   evidence clock Rung 3 depends on has not started.** The only item that moves the ladder.
8. **Decide Track O** — finish the 22 majors, or drop it. Not both fronts at once.
9. **Check whether tests can reach production Redis.** `backend/tests/conftest.py` has
   zero Redis handling and FakeRedis appears in only 4 test files, so nothing globally
   prevents `rate_limit` / `redis_lock` / `token_denylist` from building a live client
   from `settings.REDIS_URL`. Documented defaults are local, so this may be fine — one
   command settles it: `grep -E '^REDIS_URL=' backend/.env .env | cut -d@ -f2`. If the
   host is remote, `release_lock` can DEL a key a production recon worker holds.

## DECIDED — date · chose X over Y · because

Written so the next session does not re-litigate these.

- **2026-08-27 · On `feat/rolling-period` we answered a repeating gate shape with a SIBLING
  AUDIT, not a 4th patch** · because two of round 3's three majors were regressions from
  round 2's *own* fixes, and both shared one shape: *a guard applied to one path but not to
  its sibling* (ReportSeries insert got ON CONFLICT, the Report insert next to it did not;
  the resolver's GUC-restore `finally` covered the cache MISS but not the HIT). CLAUDE.md's
  PR #194 lesson is that when consecutive rounds' findings share a shape you stop looping
  and make the shape unrepresentable. So instead of only fixing the two instances, we
  enumerated every sibling of both classes on the branch: **all 3 insert sites** (
  `UserDashboardPreference` upsert, `ReportSeries`, `Report`) now carry conflict handling,
  and **all 6 `db.commit()` sites** in `dashboard.py` are followed only by in-memory
  response building. The one asymmetry left — the `report_id` branch of
  `set_active_dashboard` lacking the series branch's defense-in-depth `set_tenant_context`
  — is deliberate: it does no live I/O between its read and its write, so nothing can clear
  the GUC there. Recorded because a future reader will otherwise "fix" that asymmetry.
- **2026-08-27 · The HITL guard lives at the DISPATCHER, default-denied, over "each caller checks"**
  · because a caller that forgets is a hole, and one already existed. Verified reachable: a chat
  session with `workspace_id` set skips the guarded unified-agent block
  (`orchestrator.py:2933`, which ends in `return` at 4009) and falls through to the single-agent
  loop at 4016, whose toolset includes `ns_createRecord`/`ns_updateRecord`/`ns_deleteRecord` and
  whose only gate is `policy_evaluate` — SQL params and row limits, never mutations.
  `classify_mutation` appears **zero** times in orchestrator.py. So a workspace-attached session
  could write to PRODUCTION NetSuite with no card, no HMAC, no approval — contradicting CLAUDE.md's
  own stated invariant. Pre-existing; `agent-graph.md` #3 had named the shape without anyone
  establishing reachability. Measured urgency before fixing: 11 workspace-attached sessions exist,
  **zero active in 60 days**. Fix: `execute_tool_call(..., human_approved=False)` refusing at the
  choke point; exactly ONE caller passes True (the approve branch, whose payload is HMAC-verified
  against what a human accepted). **The default is the mechanism** — a caller added tomorrow that
  has never heard of HITL is refused rather than trusted.
- **2026-08-27 · NetSuite tools are ALLOW-listed, over the four-name deny-list** · because the
  NetSuite tool surface is *discovered at runtime* from Oracle's MCP server
  (`session.list_tools()`, `mcp_client_service.py:164`), so any write tool Oracle exposes that
  `classify_mutation` has not heard of would pass the HITL guard and mutate production unapproved.
  `agent-graph.md` states the rule ("allow-list derived from a registry, never a deny-list"). The
  trade is asymmetric on purpose: a new READ tool being refused is visible, logged with the remedy,
  and recoverable; a new WRITE tool being allowed is an irreversible ERP mutation nobody sees until
  after. `human_approved` still passes, so it is fail-closed, not fail-permanently.
  **`_BLOCKED_RECORD_TYPES` stays a deny-list** — agent-graph.md #1 says so, and record types are a
  different axis from tool names.
- **2026-08-27 · Sandbox environment binding must be ENFORCED, not hinted** ·
  `docs/superpowers/specs/2026-08-27-sandbox-environment-binding-design.md`. `source_pin` is
  deliberately advisory and is the wrong model: on an irreversible ERP write, a hint followed 95% of
  the time is a 5% chance of hitting production when the user said sandbox. It is also unusable
  mechanically — its column was dropped in migration 067. Environment is derived server-side from
  the account id (never operator-asserted — `metadata_json['account_id']` is unvalidated free text,
  so a row labelled "sandbox" can point at production), stored NOT NULL with an explicit
  `production` backfill, re-derived at dispatch, and signed into the confirmation envelope. Three of
  the nine threat-model holes were bugs in the CURRENT system and are already fixed.

- **2026-08-25 · Required NetSuite fields are CURATED IN CODE, over "discovered at runtime
  from `ns_getRecordTypeMetadata`"** · because the runtime option does not exist. The plan
  (`docs/superpowers/plans/2026-08-19-agentic-netsuite-write-loop.md:20`) forbade hardcoding
  field names and required every requiredness fact to come from that tool. Three independent
  checks killed it: the tool returns a JSON-Schema catalog whose per-field keys are only
  `{description, format, nullable, properties, title, type, x-ns-custom-field}` — no
  `required` array, and `nullable` is `false` on **zero of 177** customer fields;
  `ns_getSuiteQLMetadata` returns the identical shape; and SuiteQL `CustomField.ismandatory`
  covers **custom fields only**, never `subsidiary`/`companyname`/`entityid`. Consequence
  while it stood: every card was `unvalidated: True` with `editable_slots: []`, so required
  field validation was inert and the agent picked values like `subsidiary` by reasoning
  alone. `backend/app/services/chat/required_field_registry.py` is the reversal.
  **The risk runs BACKWARDS from the usual one** — a missing entry is cheap (NetSuite
  rejects, the repair loop recovers), a WRONG entry blocks a write NetSuite would have
  accepted and reads to an operator as the product being broken. So entries must clear two
  bars (NetSuite really rejects without it AND it does not auto-derive), carry provenance,
  and stay OUT when unproven. Deliberately excluded, each a live trap: `subsidiary` on any
  transaction carrying an `entity` (it derives FROM the entity — required=false), `trandate`
  anywhere (NetSuite defaults it), `location`/`department`/`class` (per-account config), and
  all of `expenseReport` (never observed here). Only `customer.subsidiary` is
  account-evidenced; everything else is domain knowledge, marked as such.
- **2026-08-25 · A resolved `ask_user` slot DELEGATES a missing field to the human, over
  letting the repair loop own every gap** · because once the registry made `customer`
  actually validate, the two mechanisms met for the first time and the old ordering was
  wrong: `ask_user` is the model stating it *cannot* determine a value, so bouncing that
  exact field back to the model is the one action that cannot help — it re-proposes, burns
  its repair budget to a `stall`, and the human who could answer in one click never sees a
  card. Resolution therefore runs BEFORE the repair decision in `base_agent`, and
  `ValidationResult.with_delegated_slots()` reclassifies a resolved field from "missing" to
  "asked". Terminal things never delegate: `invariant_errors` (a closed period is not a
  question a dropdown answers) and `missing_line_required` (no line-slot mechanism in v1).
  An UNresolved hint — unknown field name, or zero options — still goes to the repair loop,
  because a card with an unfillable required field is refused by the approve path's
  slot-coverage gate and is a dead end for the operator.

- **2026-08-06 · We had been SKIPPING RUNGS on the agentic-engineering ladder** · because
  the ladder (flat capture → code graph → domain graph → single-agent loop → verifier →
  small fan-out → gated team → worktrees) says each rung needs an entry trigger and a kill
  rule, and we were running 60-agent fan-outs (rung 6) with no working single-agent loop
  (rung 3), no Stop hook, and no measured baseline. The 31×-cost finding was the bill for
  that. Corrective: fix rung 3 first, and treat "does it beat one agent on cost per
  successful outcome" as the gate for climbing at all.
- **2026-08-06 · A deterministic Stop hook backs every done-claim — `stop_guard.py`** ·
  because `/goal`'s evaluator (and any transcript-reading judge) **cannot run commands or
  read files**, so it cannot distinguish a true claim from a confident one — the
  "narrated success" failure, which is this workspace's single most repeated defect. The
  hook blocks a turn that asserts green when no verify PASS is recorded for the CURRENT
  HEAD. Narrow by design: it only fires where `scripts/verify.sh` exists, and a recorded
  PASS at that exact sha silences it regardless of wording. Honours `stop_hook_active`,
  or the session wedges. Rejected: making it a model-evaluated prompt hook — that
  reintroduces the exact weakness it exists to cover.
- **2026-08-06 · Compaction carries ground truth forward — `compact_snapshot.py`** ·
  because a summary compresses narrative and drops state: on 2026-08-05 this session hit
  100% context and branch/sha, verify status and parked branches all had to be
  reconstructed by hand, while the summary confidently carried claims that were no longer
  true. PostCompact now re-reads those facts from git at injection time (never trusting
  the pre-snapshot) and states that where it disagrees with the summary, it wins.
- **2026-08-06 · The stopping rule is now IN RUN STATE — `~/.claude/hooks/loop_state.py`** ·
  because deleting `loop.sh` was right (a script you must invoke enforces nothing) but
  nothing replaced the function it served, so for three days the iteration cap lived in
  `CLAUDE.md` prose — the precise arrangement the doctrine rejects. The claim "the hooks
  replace it" was FALSE: the two installed hooks pin a gate target and record verify runs;
  neither counts anything. Three mechanisms now, none needing the agent to cooperate:
  `capture` (UserPromptSubmit) records the session goal from the first substantive prompt,
  so there is nothing to remember to invoke; `mirror` (PreToolUse Edit|Write) prints the
  recorded goal beside the directories actually edited every 25 edits; `attempt`
  (PostToolUse Bash) counts real verify.sh runs and escalates to `ask` at 15.
  *Mirror, not blocker, on purpose:* topic drift is a judgement, not a computation — but
  the PAIR (goal, dirs-touched) is mechanical, and on 2026-08-05 it would have read
  `goal: "change how we work daily"` / `edited: backend/app/api/v1 (24)`. That is enough.
  Escalation is `ask` not `deny` because this session's brief was to REMOVE restrictions
  that degrade the work; an ask you must answer is enforcement, a deny you route around is
  an obstacle. A failed state write is LOUD, not silent — if it were silent the counters
  would reset every call and the whole mechanism would look installed while doing nothing,
  which is the absence-is-not-success trap it exists to catch.
- **2026-08-05 · Enforcement lives in HOOKS, not in shell scripts we choose to run** ·
  because a script that must be invoked is the same category as prose, just executable.
  `loop.sh` was deleted on the belief that self-imposed iteration budgets are
  unenforceable — that was wrong. `PreToolUse`/`PostToolUse`/`SessionStart` hooks run
  whether or not the agent cooperates, and 9 were already configured in
  `~/.claude/settings.json` the whole time. Split verification into PULL (verify.sh —
  fine to invoke) and PUSH (hooks — the only real enforcement).
- **2026-08-05 · MEASURED: one strong agent runs FIRST on every review; the fan-out gate
  runs second, only if the cheap pass comes back thin** · because on the round-3 diff
  (~700 lines, commit `fabd731`) one Opus agent cost 146k tokens and 12 minutes against
  the gate's 4.55M tokens and 60 agents — **31×** — and found ~44% of the distinct
  defects, including **both blockers**, plus one the gate missed entirely (the DB guard
  pins three `DATABASE_URL*` vars but leaves `REDIS_URL` to `.env`, and
  `redis_lock.release_lock` does a live `DEL` — a test could release a lock a production
  recon worker holds). It also *reproduced* its findings rather than arguing them. The
  ordering is strictly dominant: the cheap pass can only add findings, and the gate still
  runs afterwards. NOT decided: whether small diffs can skip the gate entirely — that
  needs a number I did not contaminate (I wrote the baseline prompt after reading round 3
  and aimed it at "checks that pass on broken code", which is where the blocker lived).
  Get it free on the next T2 diff: single agent first, then the gate, record the delta.
- **2026-08-05 · Count DISTINCT DEFECTS, not findings** · because round 3 reported 30
  confirmed findings that collapse to ~18 real defects — `TIER: unbound` appeared 7 times
  and the flag inversion 7 times. Volume read as thoroughness. The same run also returned
  two mutually contradictory CONFIRMED findings about the same three lines: per-finding
  verification has no view of the set, so it cannot catch inconsistency between findings.
- **2026-08-05 · Freeze the dev toolchain after the two blockers; no 4th gate round** ·
  because the pre-agreed kill rule said so and the exit reason is `done`, not `stall` —
  the question the rounds existed to answer got answered. Three consecutive rounds each
  fixed a blocker and left another of the same class (`-rf`→ERROR blindness, then
  `TIER: unbound`, then the inverted `-z "$MODE"` test). Remaining ~37 findings are
  quality work on a script only we use; triage by "does it change the exit code?" —
  almost none do. They live in OPEN, not in another round.
- **2026-08-05 · Every tooling pilot gets a KILL RULE set in advance** · because round 3
  was stopped by reaction to bad results, not against a pre-agreed threshold. A pilot
  that ends in "do not build" is a success only if the bar was set beforehand.

- **2026-08-02 · Adopt the Agent-Graph track, reject the Knowledge-Graph track** · because
  our matching is 1-hop on `order_ref` and measured graph advantage starts at ≥3 hops.
  Rejected specifically: graph DB, GraphRAG, `TenantMemoryEdge` traversal, a LangGraph-style
  rewrite, critical-path orchestration, full event sourcing, entity-resolution machinery.
- **2026-08-02 · `verify.sh` is the loop's exit condition, not my judgement** · because the
  same "tests pass, zero regressions" claim was made three times on code that would have
  crash-looped every Celery worker. Same-context self-assessment is the one verification
  pattern every source rejects.
- **2026-08-02 · No scheduled autonomous dev cycle yet; closed loop first** · because METR
  measures near-100% success under ~4 human-minutes and under 10% over ~4 hours, and this
  session produced two fleet-killing blockers semi-autonomously. Automate only steps that
  traces prove boring. The "decide what to build" step is never automated — that is
  ownership, not capability.
- **2026-08-02 · Process global (`~/.claude/`), knowledge local (repo)** · because process
  is identical across projects and currently duplicated into each one, while planning
  harness barely exists anywhere.
- **2026-08-04 · Reversals post to the CURRENT OPEN period; periods are never reopened
  programmatically** · answered by the operator (accounting). A wrong posting discovered
  after close is corrected by a reversing entry dated in the current open period. Reopening
  a closed period requires an accounting controller's approval and is therefore a HUMAN
  action outside the automated path — **no endpoint, no flag, no admin override may reopen
  a period.** The close-lock invariant stays absolute in code.
  *Consequence: the compensation design never fights our own guard, and needs no exception
  path. Materiality was left open on purpose — with current-period reversal as the
  unconditional default, materiality only decides whether a human escalates for the
  exceptional reopen. It is a controller's judgement, not a constant in the codebase.*
- **2026-08-02 · The cut test is "recoverable from the repo?", not "execution vs planning"** ·
  because the bare-body test showed an agent with no harness recovered the SET LOCAL
  landmine from a docstring in `database.py:65-73`, but could not recover the T2 gate
  policy at any capability. **Prefer a docstring next to the code over a rule in CLAUDE.md.**

## DON'T — tried, failed, stop re-proposing

- **Don't verify by inspecting.** An AST/grep check over source cannot catch a NameError,
  and a disk scan cannot catch an untracked file. Import it, run it.
- **Don't trust a gate result without reading `target` and `base`.** `target: null` means it
  reviewed the session cwd's branch, which in this repo is usually the wrong one. It
  already burned a full 59-agent run.
- **Don't claim "no regressions" without a baseline.** This repo carries pre-existing
  failures; green-vs-nothing proves nothing. `verify.sh --full` does the comparison.
- **Don't put a new file under `backend/app/workers/tasks/` without checking `git status`.**
  `.gitignore` anchored `tasks/` and silently swallowed a module. (Fixed to `/tasks/`, but
  the class of bug recurs.)
- **Don't treat one clean gate round as done.** Observed major counts across rounds on a
  single PR: 0 → 2 → 3 → 1 → 0.
- **Don't add a rule to CLAUDE.md when a docstring next to the code would carry it.**

## OPEN — needs a human, blocking something

- **`feat/rolling-period` — resume here.** Worktree
  `.claude/worktrees/feat-rolling-period`, HEAD `bbb28f8f` (verify PASS at that sha).
  **There is UNCOMMITTED work in the tree** from a round-2 gate-fix agent that was still
  running when the session ended — do NOT `git checkout --` it:
  - DONE in tree: MAJOR A (`dashboard-tracking-empty-state.tsx` + `page.tsx` +
    `dashboard-switcher.tsx`) — selecting a tracking series with no report yet used to
    dump the user on the generic "nothing published" panel *with the switcher gone*, so
    there was no way back. Also the resolver GUC-between-queries minor.
  - IN PROGRESS: **MAJOR B** — `playbooks.py` creates the `ReportSeries` row BEFORE
    `_execute_sources`, and a tool call in there can COMMIT mid-flight (OAuth token
    refresh), so a later compose failure leaves a phantom series: zero reports, no audit,
    still listed in `published_series`. Its two RED tests are already written
    (`test_compose_playbook_tracking_failed_compose_leaves_no_orphaned_series`,
    `..._series_conflicting_row_reuses_existing_not_500`) and currently FAIL — that is the
    TDD red phase, not a regression. **Intended fix:** keep a read-only SELECT pre-check
    before the compose (so a repeat compose still short-circuits cheaply) and move the
    get-or-create upsert to AFTER the compose succeeds, immediately before the Report
    insert — so the row that could be orphaned is never created.
  - Then: `./scripts/verify.sh` (full — `--quick` is not evidence), then gate round 3
    **pinned**: `Workflow({name:"code-review-multiangle", args:{target:"feat/rolling-period", base:"origin/main"}})`.
  - **Do NOT merge** — deployment agents were active as of 2026-08-27, and merging
    auto-deploys staging. The 093 collision is already resolved (main's
    `093_recon_reject_labels` landed first; this branch re-parented onto it and merged main in).
  - Gate history on this branch: round 1 → 2 majors (RLS GUC cleared by an in-request
    OAuth commit; dashboard GET doing 2 live SuiteQL calls per page load) — both fixed in
    `bbb28f8f`. Round 2 → 2 majors (the two above). Both rounds valid: target pinned,
    `codex_used: true`, 0 UNVERIFIED. Expect a round 3 to find more; that has been the
    pattern all the way through.

- **`feat/rolling-period`'s last full `verify.sh` is RED — on two auth tests this branch
  does not touch.** `test_auth_security.py::TestLoginRateLimit::test_rate_limit_blocks_after_10`
  and `test_auth.py::TestLogin::test_login_creates_audit_event`. Evidence it is NOT a
  regression from this branch: (a) the SAME code passed full verify twice earlier
  (after Task 4 and Task 5, 5642 passed / 0 new failures each time) and failed on the
  third run; (b) both tests pass in isolation and as an ordered pair; (c) the branch
  contains zero auth/rate-limiter changes; (d) my tests cannot pollute them — the audit
  assertion is scoped to its own tenant and action, and nothing here touches limiter state.
  There is no `pytest-randomly`/`xdist`, so ordering is deterministic — which makes the
  intermittency *more* suspicious, not less: it points at shared limiter state or a
  time-window boundary crossed on a slow run. **This is very likely the risk already
  logged as NEXT #9** (nothing globally prevents `rate_limit`/`redis_lock` from building a
  live client from `settings.REDIS_URL`). Do not gate or land this branch on a red
  record: get ONE clean full run, and if it recurs, treat it as the NEXT #9 investigation
  rather than a rolling-period defect.

- **TWO migrations are both numbered `093` off parent `092`, on different branches.**
  `feat/rolling-period` has `093_report_series`; `feat/recon-reject-action` (and
  `agent-graph`) has `093_recon_reject_labels`. They are schema-orthogonal
  (`report_series`/`reports` vs `reconciliation_results`), so nothing conflicts
  logically — but if BOTH merge as-is, `alembic upgrade head` sees **two heads and
  fails**, and staging auto-migrates on every main merge, so that breaks deploys
  fleet-wide. **Whichever merges SECOND must re-parent to `094` off the first**
  (linearize — never a merge migration; see `memory/feedback_merge_migration_breaks_downgrade`).
  Decide merge order deliberately, not by whoever pushes first.
  *Local-only side effect:* the shared docker Postgres now has BOTH sets of DDL applied
  but `alembic_version` stamped at `093_report_series`, so the recon worktree's
  `alembic upgrade head` cannot locate its own revision. One shared local DB cannot track
  two divergent branches — whoever works locally re-stamps to their own head
  (`alembic stamp --purge <their 093>`); no DDL is lost either way. CI and staging use
  their own databases and are unaffected.

- ~~Framework's NetSuite connection dead~~ **RESOLVED 2026-08-04.** Re-authed; connection
  `active`, health-checked 02:55, deposit sync completed 02:00, data current to 02:13
  (75,534 postings). The outage was real — failed 08-03 02:00, data frozen at 07-29 for
  four nights while recon kept completing against it — and it is the exact failure the
  liveness check in `services/ops_digest.py` was built to catch: *absence is not failure*,
  so the fan-out completed successfully while silently skipping this tenant.
  *Still uncovered: per-tenant silence inside a healthy fan-out. That is the shape that
  actually bit us, and the Beat-level liveness check does not see it.*
- ~~Frozen-period reversal policy~~ **ANSWERED 2026-08-04** — see DECIDED. Reversals go to
  the current open period; reopening needs a controller and stays out of the code.
  *One sub-question deferred, not blocking: at what materiality would a controller prefer a
  prior-period adjustment over a current-period reversal? Only matters once a reversal is
  large enough to distort the current month — ask before the first material one, not now.*
- **`.gitignore` still shadows tracked FRONTEND files.** `tasks/` was anchored on this
  branch, which immediately exposed 3 lint violations in worker modules CI had never
  linted. The same defect remains at `.gitignore:59` — unanchored `memory/` matches
  `frontend/src/components/memory/` and `frontend/src/app/(dashboard)/memory/`, shadowing
  **5 tracked files** including `memory-graph-canvas.tsx`, `learned-rules-section.tsx` and
  their two test files. Verify with `git check-ignore -v --no-index <path>` — plain
  `check-ignore` stays SILENT for tracked paths, which is why this survived this long.
  *Not fixed here: anchoring it will likely surface a batch of eslint findings on files
  that have never been linted, and that is its own task, not a rider on this one. Unknown
  and worth 5 minutes: whether vitest also skips those two test files (it uses include
  globs, so probably not) or only eslint does.*
- **Track O: finish or drop?** 22 open majors. *Blocking: nothing, but it rots.*
