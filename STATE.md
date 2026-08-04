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
| `feat/agent-graph-operating-model` | mixed | 9 commits, **not merged** | nothing — active |

Contents of that branch, honestly:

- **Process (done, clean)** — daily routine + verification standard in `CLAUDE.md`; gate
  target-check in `.claude/rules/uat-review.md`; `scripts/verify.sh`.
- **Doctrine (needs cutting)** — `.claude/rules/agent-graph.md`, 35 rules. The audit found
  rules 3 and 33–34 **contradict shipped code**, and §13–20 are law for an engine we
  haven't built. Do not treat this file as authoritative until it is cut.
- **Track O — product code (PARKED)** — ops digest + Sentry-in-workers + InstrumentedTask
  coverage. Passed no gate round cleanly: **22 open majors** from round 2.

## NEXT — ordered, with the why

1. **Cut `agent-graph.md`** to the rules that are real invariants. *Why first: it currently
   contains doctrine that would break working code if followed.*
2. ~~Cut the ceremonial layer~~ **DONE** — both false lines fixed; routine + verification
   standard moved to `~/.claude/CLAUDE.md` (global, applies to every project), removing
   3.2 KB of duplication from this repo's always-loaded context.
3. ~~Build the dev-cycle graph~~ **DONE** — `loop.sh` (stopping rule in state),
   `verify.sh` (evidence), `ship.sh` (tier computed, gate target pinned). FRAME and
   SCOPE stay human nodes on purpose: that is ownership, not capability.
4. **Split this branch.** 16 commits mixing finished process work with Track O's 22 open
   majors. The process half is ready; it should not sit behind Track O's review queue.
5. **Ship the reject endpoint + MCP tool.** The service and migration exist and are
   tested, but nothing exposes them — **no labels can be recorded yet, so the evidence
   clock has not started.** This is the only item that moves the ladder.
6. **Decide Track O** — finish the 22 majors, or drop it. Not both fronts at once.

## DECIDED — date · chose X over Y · because

Written so the next session does not re-litigate these.

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
- **Frozen-period reversal policy** — a wrong auto-post is usually found after the period
  hard-freezes, so the compensating entry collides with our own close-lock invariant.
  Needs a controller's written answer. *Blocking: Rung 2 posting.*
- **Track O: finish or drop?** 22 open majors. *Blocking: nothing, but it rots.*
