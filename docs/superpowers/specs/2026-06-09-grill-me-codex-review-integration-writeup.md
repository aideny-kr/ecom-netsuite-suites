# Wiring codex into the review gate: how `grill-me` became an independent-model review angle inside a Claude-native workflow

> **Date:** 2026-06-09
> **Status:** Shipped to `main` (`e085040`)
> **Related:** `docs/superpowers/specs/2026-06-04-grill-me-codex-cross-exam-design.md` (the skill's design), `.claude/rules/uat-review.md` (the policy), `memory/feedback_independent_model_review_gate`

## TL;DR

We made an OpenAI model (`codex`) attack our code reviews from *inside* our existing
Claude-native review workflow — without forking Claude Code, writing a plugin, or changing
anything Anthropic-side. Everything is plain text files in `.claude/`:

- a **skill** (`grill-me`) that drives `codex` read-only as an adversary,
- a **workflow** (`code-review-multiangle`) that now runs that codex pass as one of 8 review angles,
- and a **template** (`build-with-review`) that folds the whole gate into any build-workflow as an
  advisory phase.

The punchline: "injecting a skill into a workflow" is not a feature. It's an **emergent property**
of two primitives Claude Code already ships — *workflows can spawn subagents* and *subagents can
read files*. We just connected them.

---

## 1. The problem: Claude-on-Claude review shares blind spots

Our T2 review gate (`code-review-multiangle`) was 7 parallel "finder" angles, each a Claude
subagent (line-by-line, removed-behavior, cross-file, reuse, simplification, efficiency, altitude).
It's good, but every angle is the *same model family* reviewing the same diff — so a class of bug
Claude is blind to survives all 7 angles.

We had already learned this the hard way (`memory/feedback_independent_model_review_gate`): on the
metric-catalog work, an independent model (`codex`) caught 7 real defects that multiple adversarial
Claude rounds missed. The standing rule became "run grill-me/codex before claiming PR-ready." The
gap was that this was a *manual* step a human had to remember, sitting outside the automated gate.

**Goal:** put a genuinely different model inside the gate so the review is no longer
Claude-on-Claude.

---

## 2. What `grill-me` is

`grill-me` is a skill (`.claude/skills/grill-me/SKILL.md`). In **diff mode** it:

1. Preflights `codex` (binary present? authed?) and defines a filesystem boundary so codex ignores
   our `.claude/` skill files.
2. Runs `codex exec -s read-only` against the diff with a cross-exam prompt tuned to this repo's
   failure modes (tenant isolation, LLM-presented numbers, HITL mutation guard, SuiteQL dialect,
   prompt pollution, soul config).
3. Triages each finding, escalates only what survives, and (interactively) loops for multiple rounds.

The skill is the **canonical definition of "how to drive codex"** — the exact flags, the
`codex-cli 0.134.0` version quirks (options must precede the positional prompt; never `resume`, so
`-s read-only` holds every call), and the auth-noise filtering (codex's own MCP transport errors
like `rmcp::transport` / `suitetalk` are false positives, not CLI auth failures).

---

## 3. The mental model: primitives vs. custom

The reason this is "all custom" is a clean separation:

| Layer | Who owns it | Examples |
|-------|-------------|----------|
| **Primitives** (fixed extension points) | Claude Code ships them | `Workflow` tool (JS scripts calling `agent()` / `parallel()` / `workflow()`), skill discovery from `.claude/skills/`, subagents with real tools (Bash, Read), `settings.json` hooks |
| **Behavior** (100% custom) | Lives in your repo as text | `SKILL.md`, the workflow `.js` files, `.claude/rules/*.md`, `CLAUDE.md` |

Anthropic gives you the sockets. The behavior is yours — readable, diffable, revertible, and it
travels with the git branch.

**The hard constraint that shaped everything:** the Workflow JS runs in a sandbox with **no
filesystem and no shell** — it can only call `agent()`, `parallel()`, `log()`, `phase()`, and
`workflow()`. It *cannot* read a `SKILL.md` or invoke the Skill tool. But the **subagents it spawns
via `agent()` do have Bash + Read.** That one fact is the whole hinge.

---

## 4. How we attached it — the five steps

Each step is a real commit on `main`. The interesting part is steps 2→4, where the first working
version got progressively de-duplicated.

### Step 1 — merge the skill (`c9a7759`)
Brought `.claude/skills/grill-me/SKILL.md` onto `main`. Now both an interactive session (`/grill-me`)
and any subagent can reach it.

### Step 2 — add a codex angle, inline (`04b6998`)
Added an 8th angle to `code-review-multiangle.js`. Because the JS can't run codex itself, the angle
is an `agent()` whose **prompt tells a subagent** (which has Bash) to run `codex exec -s read-only`
against the diff and return structured candidates. First working version — but it carried an
**inline copy** of the codex recipe (flags, boundary, fallback). It worked, but it was a second
source of truth that would drift from the skill.

Design choices baked in here and kept throughout:
- **Graceful fallback:** if codex is missing/unauthed, the angle degrades to a hostile *Claude-only*
  persona and reports `codex_used: false` — the gate never *silently* becomes Claude-only.
- **Fail-closed only on death:** a fallback still "ok"s the angle (so codex-less hosts don't force
  `INCOMPLETE`); only an actual agent crash marks the angle failed → `INCOMPLETE`.
- **`codex_used` surfaced** in the result so a reviewer knows whether a real second model ran.

### Step 3 — harden, by grilling our own change (`b56586f`)
We dogfooded: ran `codex` read-only against the codex-angle diff itself. Codex flagged three real
issues, which we fixed:
- `codex_used` was set via a `parallel()` closure side-effect → made it derive from the returned
  result instead (no dependence on execution-order/retry semantics).
- the subagent transcribed the diff to a file with no completeness check → added a truncation
  tripwire.
- `timeout` may be absent on minimal runners → use `timeout`/`gtimeout` only if present; treat
  exit 124/127 as codex-unavailable → fallback.

### Step 4 — refactor to *delegate* to the skill (`1919c73`)  ← the key move
The inline recipe was the duplication we wanted gone. We deleted it and replaced the angle's prompt
with: **"Read `.claude/skills/grill-me/SKILL.md` and follow its codex procedure."**

```
workflow JS  →  agent("...Read .claude/skills/grill-me/SKILL.md and follow it...")
                      ↓
                 subagent uses its Read tool on that file
                      ↓
                 follows the markdown (runs codex per the skill's exact command)
```

Now the workflow carries **zero** copy of the codex recipe — only the workflow-specific glue: which
diff to review, the output contract (`{candidates, codex_used}`), and the gate overrides (one pass,
no user escalation, no artifact). `SKILL.md` is the single source of truth.

We validated by spawning the angle's subagent against this very diff. It read the skill, lifted the
codex command **verbatim**, and ran real codex. Bonus: the skill's command turned out *richer* than
our old inline copy (it has `--json` stream parsing + `web_search`), so delegating actually
**upgraded** the angle. The test also surfaced two fixes folded into the same commit: re-reference
the existing `REPO_INVARIANTS` const in the fallback (reuse, not new duplication) and a `$TMPDIR`
sandbox-retry note (see §6).

### Step 5 — make the gate composable into build-workflows (`e085040`)
The last piece: have the review fire *automatically* as part of building. Because `workflow()` can
run another workflow inline (nests one level), a build-workflow can end with:

```js
phase('Diff')                                              // an agent computes the built diff (JS can't run git)
const { diff } = await agent('return `git --no-pager diff HEAD`…', { schema: { /* {diff} */ } })

phase('Review')                                            // the gate runs as a phase of the build
const review = await workflow('code-review-multiangle', { diff })   // args.diff is TRUSTED → bypasses the gate's prep
log(`review: ${review.status} codex_used=${review.codex_used} findings=${(review.findings||[]).length}`)
return { review }                                          // ADVISORY: attach findings, do NOT throw — build completes
```

Shipped as the copy-me template `.claude/workflows/build-with-review.template.js`, documented in
`.claude/rules/uat-review.md`, and pointed at from `CLAUDE.md` so workflow-authoring agents add the
phase. Policy choice: **advisory** (report, never block); flip to blocking by `throw`ing on
`INCOMPLETE` or on a `blocker`/`major` finding.

---

## 5. The result: one source, three consumers

The same `SKILL.md` is now used three ways, with no duplicated logic:

```
.claude/skills/grill-me/SKILL.md  (single source of truth for "how to drive codex")
        │
        ├─ /grill-me                          interactive, full multi-round cross-exam
        ├─ code-review-multiangle (angle 8)   one codex pass folded into the T2 gate
        └─ build-with-review (Review phase)   the gate folded into any build-workflow, advisory
```

---

## 6. Hard-won details (the edges of the model)

- **JS can't read files; agents can.** The entire "injection" rides on pushing the file-reading
  down into the subagent. The instruction to use the skill still lives in the JS — as the agent's
  prompt string.
- **It's prompt-*following*, not `import`.** The subagent interprets the markdown and can deviate;
  that's why the angle pins overrides (one pass, no escalation) explicitly.
- **The skill must be on disk in the reviewed checkout.** If a branch lacks
  `.claude/skills/grill-me/`, the angle can't read it and falls back to Claude-only even when codex
  is installed. Trade-off accepted for zero duplication; surfaced via `codex_used`.
- **Sandbox + network + `$TMPDIR`.** codex needs OpenAI network, which the command sandbox blocks,
  so its call fails sandboxed and must retry unsandboxed — and `$TMPDIR` differs across that
  boundary, so the prompt file must be **built and consumed in the same unsandboxed invocation**.
  Success signal is codex's `agent_message` / "tokens used", not the piped shell exit code.
- **Writing under `.claude/skills/` is itself sandbox-protected** — git operations touching it need
  the sandbox disabled.

---

## 7. Verification (end-to-end, live)

We ran the template against a deliberate 10-line diff (a throwaway file with a planted empty-array
null-deref). The nested call returned:

```json
{ "review": {
    "status": "OK",
    "codex_used": true,                ← the decisive proof
    "diff_lines": 10,
    "failed_angles": [],
    "total_candidates": 11, "confirmed": 1, "kept": 6,
    "findings": [ /* incl. explicit "codex finding" entries */ ]
}}
```

What it proved: the nested `workflow('code-review-multiangle', {diff})` resolved **by name** to the
*updated* 8-angle gate (the `codex_used` field only exists there), real codex ran inside it
(`true`, not the fallback), codex's findings flowed through, the planted bug was caught, and the
build completed advisorily (no throw). Cost: ~861k subagent tokens / 20 agents / 165s — the real
per-run price of an in-loop independent review, worth knowing before high-frequency loops.

---

## 8. How to use it

- **Manual T2 gate:** `Workflow({ name: "code-review-multiangle", args: { target: "<PR#|branch>" } })`.
  Read `status` first (`INCOMPLETE`/`PREP_FAILED`/`EMPTY_DIFF` ⇒ re-run), then check `codex_used`
  (`false` ⇒ codex fell back on that host ⇒ weaker pass; re-run where `codex login` works).
- **Inside a build-workflow:** copy `.claude/workflows/build-with-review.template.js`, replace the
  Implement phase with your real build agents, keep the Diff + Review phases.
- **Interactive:** `/grill-me` for the full multi-round cross-examination before a PR.

---

## 9. The generalizable pattern

Nothing here is codex-specific. The reusable shape is:

> A workflow agent can be pointed (via its prompt) at any instruction file — a `SKILL.md`, a rules
> doc, a checklist — and told to read and follow it. Your skills library doubles as a library of
> workflow building blocks, consumed identically by interactive sessions (Skill tool) and by
> workflow subagents (plain Read), from one source of truth.

If you later want the review to fire on *every* session (not just workflow-orchestrated builds),
that's the other primitive — a `Stop` hook in `settings.json` — and a separate build. Hooks run
shell, so a hook can't call the Workflow tool itself; it would *compel* a session to run the gate.
