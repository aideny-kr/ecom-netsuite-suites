# grill-me (codex cross-examination) — Design

> Date: 2026-06-04
> Status: Approved design, ready for implementation plan
> Type: Project skill (`.claude/skills/grill-me/`)

## One sentence

Claude states its understanding of a target (a plan/spec, or a diff), codex repeatedly
attacks that understanding until no new gap survives, and only the gaps neither model can
resolve from the codebase get escalated to the user — producing a hardened-understanding doc
plus the cross-exam transcript.

## Why this exists

Canonical `grill-me` (Matt Pocock lineage) is *Claude interviewing the user* one question at
a time. That overlaps with `superpowers:brainstorming` and is limited by Claude's own blind
spots — Claude asks about what Claude already thought to question.

This version flips the adversary: **codex (a different model, "200 IQ second opinion") attacks
Claude's reasoning before the user is ever asked.** The user is only pulled in for gaps that
survive an independent model's attack AND can't be resolved by reading the code. That makes the
user's time the scarcest resource and raises the floor on what "shared understanding" means.

## Decisions (locked)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Codex role | Cross-examines Claude's reasoning | Independent model surfaces gaps Claude can't self-generate |
| Target | Mode-detected: plan/spec OR diff | Pre-implementation hardening + intense code-quality review |
| Loop | Loop until no new surviving gap (cap 3 rounds) | Most rigorous; convergence not single-shot |
| Escalation | Only unresolvable gaps, one at a time | Protect user time; keep grill-me one-question DNA |
| Output | Hardened-understanding doc + codex transcript | Durable record; plan mode updates the spec |
| Codex wiring | Call `codex` CLI directly (self-contained) | No dependency on gstack codex skill being installed |
| Codex-missing | Graceful fallback to Claude-only self-cross-exam | Never dead-ends; still produces value without codex |
| Location | `.claude/skills/grill-me/` (checked in) | Shared with team + agents; bakes in repo invariants |
| v1 scope | Both modes (plan/spec + diff) | Matches the full "both" intent |

## Trigger & invocation

- Path: `.claude/skills/grill-me/SKILL.md`
- Triggers: "grill me", "grill this plan", "cross-examine this", "/grill-me"
- Read-only against the repo except for the single output artifact it writes.

## Mode detection (Step 1)

1. **Plan/spec mode** — an active plan file is referenced in the conversation, OR the user
   names a file under `docs/superpowers/specs/*.md` / `docs/superpowers/plans/*.md`, OR the
   user describes an idea verbally. Goal: harden understanding *before* implementation.
2. **Diff mode** — a diff exists vs the base branch (`git diff origin/<base> --stat`). Goal:
   intense adversarial code-quality review of the change.
3. Both present → `AskUserQuestion` to pick. Neither → ask the user to point at a plan or
   describe the work.

Base-branch detection reuses the codex skill's Step 0 logic (gh/glab/git-native fallback to
`main`).

## The cross-examination loop (Step 2 — core)

```
round = 0; max_rounds = 3; codex_session = none; escalated = []

# Phase A — Claude commits to an understanding
STATED_UNDERSTANDING = Claude reads target + relevant code, writes:
  - plan mode: what the work is, key decisions, assumptions, invariants relied on,
    success criteria
  - diff mode: what the change does, why, claimed safety/correctness properties,
    what it assumes about callers/data

# Phase B — codex attacks, loop to convergence
while round < max_rounds:
    attacks = codex_cross_exam(STATED_UNDERSTANDING, resume=codex_session)
    codex_session = captured thread id   # persistent across rounds
    new_surviving = 0
    for attack in attacks:
        if resolvable_from_codebase(attack):
            Claude reads code → rebut (dismiss) or concede (update
            STATED_UNDERSTANDING).            # no user involvement
        elif needs_user_intent_or_decision(attack) and attack not already escalated:
            escalated.append(attack); new_surviving += 1
    round += 1
    if new_surviving == 0:
        break                                 # converged

# Phase C — escalate to user
for gap in escalated:                         # one at a time
    ask user with Claude's recommended answer
    fold answer back into STATED_UNDERSTANDING
```

### codex_cross_exam() invocation

Built directly on the codex CLI mechanics (mirrors the gstack codex skill, self-contained):

```bash
codex exec "<cross-exam prompt>" \
  -C "$_REPO_ROOT" -s read-only \
  -c 'model_reasoning_effort="high"' \
  --enable web_search_cached --json < /dev/null 2>"$TMPERR" | <jsonl streaming parser>
```

- Round 0 starts a new thread; capture `thread_id` from the `thread.started` event.
- Rounds 1..N use `codex exec resume <thread_id> "<prompt>"` so codex remembers what it
  already attacked and conceded — avoids repeating itself, drives true convergence.
- Hardening carried over from the codex skill: binary check, auth probe (`$CODEX_API_KEY` /
  `$OPENAI_API_KEY` / `~/.codex/auth.json` / `codex login`), version check, `timeout` wrapper
  (600s) with exit-124 hang detection, stderr auth-error surfacing, and the **filesystem
  boundary guard** (forbid codex from reading `~/.claude/`, `.claude/skills/`, `agents/`).
- **Skill-rabbit-hole detector**: if codex output mentions `gstack-config`, `SKILL.md`, etc.,
  warn that codex got distracted and offer to retry.

### Cross-exam prompt shape

The prompt sends codex **Claude's stated understanding**, not just "review the code":

> Here is another AI's stated understanding of [this plan / this diff]. Attack the
> *reasoning*: which assumptions are wrong or unverified, which decisions are unexamined,
> which gaps would bite in production. Don't review the code in a vacuum — challenge what
> the other AI claims to understand. Be adversarial, terse, no compliments.

Plus repo-specific attack lenses (§ Project invariants).

## Escalation to the user (Step 3)

- Only gaps that survived codex's attack **and** cannot be resolved by reading the code.
- Asked **one at a time**, each with Claude's recommended answer (grill-me DNA).
- Codex's raw per-round output is shown verbatim (never summarize the adversary); Claude's
  triage and synthesis come *after* the verbatim block.

## Project invariants as attack lenses (§5)

The cross-exam prompt explicitly arms codex with this repo's real failure modes, so the
grilling is tuned to where *this* codebase breaks:

- Multi-tenant: every table has `tenant_id`; RLS via `SET LOCAL app.current_tenant_id`. Attack
  any path that could leak across tenants.
- Never let the LLM present tool-computed numbers (SSE interception). Attack any place a number
  could be hallucinated/rounded by the model.
- MCP write safety: `ns_createRecord`/`ns_updateRecord` must go through HITL mutation guard;
  system record types blocked. Attack any auto-execute path.
- SuiteQL dialect: local REST supports `customrecord_*`; external MCP only standard tables.
  Attack dialect/source-kind mismatches.
- No prompt pollution: no hardcoded column names/schema in prompts or golden datasets.
- Soul config is sacred: never overwrite/seed `/tmp/workspace_storage/{tenant_id}/soul.md`.

These are passed as a checklist in the codex prompt and also used by Claude when triaging
attacks in diff mode.

## Output artifact (Step 4)

- **Plan/spec mode:** `docs/superpowers/specs/YYYY-MM-DD-<topic>-grilled.md` (or appends a
  `## Hardened by grill-me` section to the named spec if one was the target).
- **Diff mode:** `.claude/grill-reviews/<branch>-<YYYY-MM-DD>.md`.

Contents:

1. **Hardened understanding** — final decisions, assumptions, invariants, success criteria.
2. **Cross-exam transcript** — per round: codex's attacks, what survived, what Claude resolved
   from code (with file:line), what the user decided.
3. **Verdict** — `CONVERGED` (no new gaps) or `ROUND-CAP` (hit 3 rounds with open gaps listed).

## Failure & edge handling

- **Codex missing / auth fail** → state it, then degrade to **Claude-only self-cross-exam**
  (Claude writes the understanding, then adopts an adversarial persona to attack its own
  reasoning across the same loop). Mark the transcript `FALLBACK: claude-only` so the weaker
  guarantee is visible.
- **Not a git repo / no diff in diff mode** → fall back to plan mode or ask.
- **codex timeout (exit 124)** → surface the actionable hang message, let the user retry; do
  not silently swallow.
- Read-only throughout; the only write is the output artifact.

## Validation (acceptance gate)

A `SKILL.md` is a prose/config artifact, so per the project TDD rule (which exempts purely
non-code artifacts) we validate by **dry-running the skill on a real target** rather than unit
tests:

1. Point it at an existing spec in `docs/superpowers/specs/` (plan mode) → confirm mode
   detected, codex actually runs and attacks the stated understanding, loop terminates at
   convergence or round cap, hardened doc written with a transcript.
2. Point it at a real branch diff (diff mode) → same, producing a `.claude/grill-reviews/`
   record.
3. Temporarily simulate codex-missing (PATH without codex) → confirm graceful Claude-only
   fallback runs and the transcript is marked `FALLBACK: claude-only`.

## File layout

```
.claude/skills/grill-me/
  SKILL.md            # the skill (frontmatter + the workflow above)
```

Single file is the target. A helper script is only added if the JSONL parsing / loop bash
proves too large to inline cleanly — decided during planning, not assumed here.

## Non-goals (YAGNI)

- No telemetry / gstack preamble / brain-sync (this is a lean project skill, not a gstack
  skill).
- No session continuity *for the user* across invocations (codex session is per-run only).
- No auto-fixing of findings — grill-me hardens understanding and records gaps; fixing is a
  separate step (TDD implementation, `/codex review`, etc.).
- No parallel multi-model panel beyond codex in v1.
