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
| `feat/rolling-period` | T2 | **SHIPPED** — squash-merged as main `f74b781f` (PR #209), deployed + live-verified on staging (backend recreated, `alembic current`=`094_dashboard_preference_series` head, FE digest `4a37ebf8`, BUILD_ID baked). Gate ×4: majors 2→3→0 | nothing |
| `feat/batch-write-idempotency` | T2 | **BLOCKED ON A DESIGN CALL — do not merge.** verify PASS @ `27f2ad15`. Gate ×4 (ceiling reached): rounds found 1 shipped blocker (line items stripped from every transaction write), 1 blocker I had wrongly declared fixed, and ~18 real defects. Round 4 was the strongest (`codex_used=true`) and still found a FOURTH variant of one shape. See OPEN → "one key, three jobs" | a human's design decision | Phase 1 of the batch-write plan, shipping alone: work-derived idempotency key in `externalId` + side-effect log committed BEFORE the call + settle-only-on-a-definite-answer. `kill -9` drill run in both branches. Also fixes the single-record case — a timed-out write is now answerable instead of reported `failed` and offered for blind retry | verify + T2 gate |
| `feat/rolling-period-stage2` | T2 | Scheduled compose built: daily Beat sweep, reason enum → `jobs.result_summary`, per-tenant cost ceiling, waiting ribbon lit (DATA-gated on the sweep being enabled). verify **PASS @ `fa793ce6`** (+15 tests). **T2 gate round 1 in flight** | gate verdict → PR |

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

**Batch NetSuite writes — scoped 2026-08-31, mock APPROVED, not built.**
Spec: `docs/superpowers/specs/2026-08-31-batch-write-idempotency-design.md`.
**Design reference (anti-drift): `docs/superpowers/specs/batch-write-review-mockup-reference.html`**
— when implementation and mock disagree, decide deliberately and update both in one commit.

Order is load-bearing, not preference:
1. **Idempotency key + side-effect log written BEFORE the call.** Blocks everything else
   (agent-graph #10, explicitly unbuilt). Ships alone and is independently valuable — it
   fixes the single-record timeout case too. FIRST unknown to settle: does NetSuite REST
   support a client-supplied idempotency key? If not, reconcile on natural identity
   (externalId), which changes the schema. Establish before writing it.
2. **Deterministic server-side extraction** — operator decision 2026-08-28: model
   transcription is fine for ONE record, never for a batch.
3. **The review surface** per the mock — every row's values shown, environment badge
   loudest, resume BLOCKED on any `Unknown` row.
4. **Server-enforced cap** in code, not prompt.

*Rejected, do not re-propose: "approve once, auto-approve the rest".* The approval would
precede the payloads — consent to content nobody has seen. PR #210's duplicate bug was
survivable only because a human was there to reject the identical retry; under
auto-approve it writes the duplicate silently, every row. Batch review costs the same one
click and keeps the approval attached to rows actually seen.

**Multimodal record creation (2026-08-28).** User asked for: upload xlsx/pdf/csv/photo →
agent proposes NetSuite records → existing HITL card. Research (7-agent survey) found upload,
storage, `file_id` plumbing and an injection-hardened preview ALREADY EXIST for
xlsx/csv/xls/json. Slice ordering chosen by the user, recorded so it is not re-litigated:

1. ~~**Slice 1** — one file → one record~~ **CERTIFIED LIVE 2026-08-28.** Driven end to end
   on staging: a 1-row CSV produced a card targeting `SANDBOX 6738075-sb1` with every value
   transcribed correctly and `subsidiary` resolved from the NAME "Framework Computer UK Ltd"
   to internal id 5, then labelled back. Needed no new code — it was certify, not build.
2. **Slice 2** — `task_file.read` tool, so the agent sees past the 20-row/12-col preview.
   BUILT on branch `feat/read-task-file-tool` (`ef9ee29e`), **unpushed, unmerged,
   `verify.sh` NOT-DONE** (celigo flakes, see OPEN). **CERTIFIED LIVE 2026-08-29** — a
   60-row xlsx with a random token planted at row 47 (past the 20-row preview): the agent
   returned the exact token, which is unobtainable without reading the file.
   *Two things this cost, both worth remembering: (a) the tool's signature was wrong
   — the dispatcher calls `execute_fn(params, context=context)` with db INSIDE context,
   so every live call died while 17 unit tests passed, because the test helper had invented
   its own calling convention; (b) the first two certification designs were answerable from
   the preview (CSV previews are not row-capped, and `Vendor NN Ltd` is extrapolable), so
   "correct answer, zero tool calls" was the result. The third design planted an unguessable
   value.*
   *Correction: I twice claimed the frontend picker rejects `.xls`. It does not —
   chat-input.tsx accepts it at both the handler and the accept attribute. The REAL gap was
   the opposite: the tool advertised `.xls` while openpyxl cannot read legacy binary .xls
   (BadZipFile) and xlrd is not a dependency. Now refused by name with a re-save remedy.*
   *Note: a successful tool call logs NOTHING at the container's effective level (only
   errors do), so `grep task_file.read` in docker logs is a false negative — do not read
   its absence as "the tool was not called".*
3. **Slice 3** — text-layer PDF via pdfplumber (already a dependency, wired only into
   drive_rag). Scanned PDFs must fail with an honest "no text layer", never a guess.
4. **Slice 4** — small-N sequential proposals, cap ≤10 enforced in CODE, shared
   `correlation_id`. **BLOCKED** until the idempotency key + pre-call side-effect log exist
   (agent-graph.md #10 — explicitly unbuilt).
5. **Slice 5+** — true batch review surface, and photos via Anthropic vision (adapter-gated;
   OpenAI/Gemini adapters are text-only). Both need mock-first design per report-design.md.

**Decided, do not re-open:** values may pass through the model for a SINGLE record (the human
reads every field on the card); deterministic server-side extraction is a hard precondition of
any BATCH slice, because nobody eyeballs 200 rows. No OCR dependency for an accounting write
path — OCR confidence is unquantified and the card cannot show what was misread.

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

- **2026-09-01 · Idempotency rides in `externalId`, NOT in a request header** · because
  there is no header channel to ride in: `ns_createRecord` accepts exactly `recordType`
  and `data`, read from the live MCP server's own schema, so a header-based scheme was
  not a worse option — it was an impossible one. `externalId` is strictly better than the
  header would have been: NetSuite enforces uniqueness **server-side**, so the guarantee
  is not trusted from us. Measured live in the sandbox, not read in docs: identical create
  twice → `recordId 5264548`, then HTTP 400 "This entity already exists", then a count of
  1. A blind retry therefore cannot duplicate, and hitting the refusal *proves* the
  original landed — which is why `classify_retry_result` maps a duplicate refusal to
  WRITTEN rather than to an error.
- **2026-09-01 · The payload is stamped at CARD BUILD, before the HMAC — never at
  execution** · because stamping after approval would make the executed payload differ
  from the approved one, which is the precise defect this branch already fixed once. The
  key is therefore part of what the human sees and what the signature covers. Creates
  only: an update targets an existing `recordId`, so stamping there would mutate a
  business field for no benefit. A caller-supplied `externalId` is never overwritten —
  their key is the better natural identity, and replacing it would corrupt an integration
  we do not own.
- **2026-08-30 · Stage 2 gates the RIBBON DATA on the scheduler being enabled, not just the
  wording** · because the amber ribbon promises a statement "is scheduled", and a promise
  about a background job is only as true as the job's on/off switch. The approved Stage 1
  mock said "building X's statement now"; on a daily cadence that is false for up to a day,
  which is the SAME defect the T2 gate caught in the Stage 1 launcher copy ("composed
  automatically...", no scheduler behind it) — the identical lie relocated to another
  component. Two changes, and the second is the load-bearing one: the copy now says
  "scheduled and will appear within a day", AND `closed_days_ago` is withheld entirely
  unless the series is behind AND `ROLLING_PERIOD_AUTO_COMPOSE_ENABLED` is on. Wording
  drifts; a withheld field cannot lie. The FE must therefore keep gating amber on the
  field's PRESENCE and must never derive it by comparing `period`/`resolved_period` —
  deriving it client-side puts "is scheduled" on a deployment where nothing is scheduled.
  Recorded because deriving it looks like a harmless simplification.

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

### The idempotency key is doing three jobs at once (2026-09-02) — BLOCKING `feat/batch-write-idempotency`

Four T2 gate rounds each found a fresh variant of ONE defect. Listing them in order,
because the pattern is the finding:

| round | variant |
|---|---|
| 1 | key derived from `.fields` — line items didn't participate; salesOrders posted header-only |
| 2 | `record_type`/`connector`/`mutation_type` absent; payload-less mutations hashed to a CONSTANT; key frozen at card build while slot-fill changed the payload |
| 3 | the round-2 fix never reached the call site (un-asserted `.replace()`); `record_id` read from a different source than the card used |
| 4 | `record_id` dropped when the record is empty but an id exists; `merge_slot_values` still never recomputes; a synthesized key trusted as "sent" when it never was |

**Root cause — one value, three jobs, three different validity conditions:**

1. **NetSuite-enforced dedupe.** Must be INSIDE the payload as `externalId`. Only works for
   CREATES (`base_agent.py:2208` gates stamping on `mutation_type == "create"`), and only if
   the payload is final when stamped.
2. **Ledger row identity.** `write_side_effects` needs a key for EVERY mutation type,
   including the updates and deletes that job 1 never stamps.
3. **Proof-of-landing.** "This entity already exists" ⇒ WRITTEN — valid ONLY if the key was
   actually transmitted.

Every defect above is these three pulling apart. The sharpest: the orchestrator synthesizes
an `ss-idem-` key for an UPDATE (job 2), nothing stamps it into the payload (job 1 is
create-only), and then `classify_retry_result` sees the `ss-idem-` prefix and treats a genuine
"already exists" as proof our write landed (job 3) — **settling a failed update as `written`,
irreversibly.** `reconcile_by_external_id` already implements the correct guard for this
(agent-graph.md 12a: only conclude from a key that was actually SENT); `classify_retry_result`
does not. Fixing it there too would be patching the second of N call sites — the shape this
repo has been bitten by repeatedly.

**Two ways forward. This is a design decision, not a bug fix, which is why it is here:**

- **A — split the concept (bigger, correct).** A `ledger_key` that always exists and is never
  sent, plus a nullable `netsuite_external_id` set ONLY when actually stamped into a create.
  Proof-of-landing becomes conditional on that column being non-null and matching
  `payload_json` — structurally, not by a prefix check. Recomputation then only concerns the
  external id, only up to approval.
- **B — narrow phase 1 to creates (smaller, shippable now).** Stamping is already create-only;
  make the LEDGER create-only to match, and refuse to settle non-create mutations through this
  path. Kills the job-2/job-3 conflict outright. Costs the update/delete audit trail until A
  lands.

**Recommendation: B now, A as its own slice.** The branch's real value — a timed-out CREATE
becomes answerable instead of being reported `failed` and offered for blind retry — is
delivered entirely by B, and B is a deletion rather than an addition.

Not to be re-litigated: the key must stay in `externalId` (there is no header channel — the
MCP tool takes only `recordType` and `data`), and NetSuite's server-side uniqueness is the
guarantee. Both measured live, see DECIDED.



- **`feat/rolling-period-stage2` — resume here.** Worktree
  `.claude/worktrees/feat-rolling-period` (branch switched), verify PASS @ `fa793ce6`.
  Stage 1 is SHIPPED and live; this branch is Stage 2. NOTE: a re-parented migration can
  strand the local DB at a head that SKIPS main's newer migrations, so `alembic upgrade
  head` no-ops and ~123 celigo tests "fail" — repair with stamp/upgrade/stamp, see memory
  `reference_worktree_db_stranded_behind_reparented_migrations`.
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
- **ROTATE THE `gh` OAUTH TOKEN — leaked twice on 2026-08-28, by me, both times the same way.**
  The token is `gh auth token` for `aideny-kr` (scopes: repo, write:packages, read:org, gist).
  Leak #1: authenticating the staging VM to GHCR, I wrote
  `TOKEN=$(gh auth token); ssh vm "echo '$TOKEN' | docker login …"` — interpolating it into
  the ssh command string puts it in the REMOTE process's argv, readable via `ps aux` on the
  VM. Leak #2: after the user rotated, I verified the new token *using the same construction*,
  burning the fresh one within minutes of having described the hazard. It is also in this
  session's transcript `.jsonl` in plaintext, permanently.
  **Do:** revoke at github.com → Settings → Applications → Authorized OAuth Apps → GitHub CLI
  (logout alone may not revoke server-side), then `gh auth login` and
  `gh auth refresh -h github.com -s write:packages` — the deploy needs write:packages for GHCR.
  **The only safe form, use it every time:**
  `gh auth token | ssh aidenyi@34.73.236.64 "sudo docker login ghcr.io -u aideny-kr --password-stdin"`
  — the secret travels on stdin and never appears in any argv.
  **The real lesson:** knowing the rule did not prevent the leak; I recited it and then broke
  it because the unsafe form was one line shorter. Never put a secret in a shell variable that
  a later command can interpolate — pipe it, or it will eventually end up in argv.
- **Three test customers still active in PRODUCTION NetSuite (6738075)** — internal ids
  `5803124`, `5800803`, `5795008`, created while proving the write path. Harmless but real
  records in a real ledger; inactivate them. (Sandbox `6738075-sb1` also holds test rows —
  `5264348` "Sandbox Smoke Test", plus the Card Rate Probe / Northwind Slice One rows — those
  are sandbox and can stay.)
- **`test_celigo_flows_api.py` leaks state from somewhere in the wider suite — UNLOCATED.**
  Fails only in a full run, naming DIFFERENT tests each time while head and baseline failure
  totals stay identical (123 = 123). Ruled out with evidence on 2026-08-30: in-file ordering
  (3/3 clean alone), `tests/api` siblings (2/2 clean, 195 tests), and the module-level
  `_FLAG_CACHE` in feature_flag_service (131 passed with four flag-mutating files ordered
  first). The leaking file is elsewhere in ~5000 tests and bisecting costs ~6 min a run.
  *Mostly defused rather than fixed:* `verify.sh` now re-runs each newly-failing test in
  isolation, so a flake no longer produces a false "these are yours". Still worth locating —
  a test that only fails in company can also hide a real interaction bug.
- **Never deploy an unmerged branch to shared staging.** Any main merge auto-deploys and will
  silently replace it — PR #209 did exactly that at 01:00:46 on 2026-08-31, wiping a build
  whose live certification had passed 40 minutes earlier. Three failure modes: others testing
  staging get YOUR branch (including the write path); your live certification decays the
  moment someone merges; and your manual deploy can clobber their freshly-merged feature.
  If a live test genuinely needs it, record `git rev-parse origin/main` BEFORE and AFTER and
  treat any change as invalidating the result. "Verified live" needs a timestamp and a
  main-sha beside it, not a checkmark.

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
- **`.gitignore` also shadows `docs/` — every spec in the repo was force-added.**
  `docs/` appears TWICE (`.gitignore:51` and `:69`) while ~40 files under
  `docs/superpowers/specs|plans` are tracked. So a new spec silently fails to commit unless
  you spot the "paths are ignored" hint and reach for `-f`; miss it and the design doc you
  just wrote is simply absent from the branch. Hit on 2026-08-31 filing the batch-write
  spec. A proper fix needs the un-ignore chain (`docs/*` + `!docs/superpowers/` +
  `docs/superpowers/*` + `!docs/superpowers/specs/`…) and would surface whatever else under
  `docs/` is currently hidden on purpose — check that before changing it, and do it as its
  own commit rather than riding along with feature work.
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
