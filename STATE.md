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
