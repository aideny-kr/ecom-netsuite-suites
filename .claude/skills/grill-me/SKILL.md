---
name: grill-me
description: >-
  Cross-examine an understanding before committing to it. Claude states what it
  believes about a plan/spec or a diff, then codex (an independent model) attacks
  that reasoning round after round until no new gap survives. Only gaps neither
  model can resolve from the codebase are escalated to you, one at a time.
  Produces a hardened-understanding doc plus the codex cross-exam transcript.
  Use when asked to "grill me", "grill this plan", "cross-examine this", or before
  starting implementation / opening a PR when you want a second model to find the
  holes first.
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - AskUserQuestion
---

# grill-me — codex cross-examination

**One sentence:** Claude states its understanding of a target, codex repeatedly attacks that
understanding until no new gap survives, and only the gaps neither model can resolve from the
codebase get escalated to you — producing a hardened-understanding doc plus the transcript.

This is NOT canonical grill-me (Claude interviews you). Here **codex is the adversary that
attacks Claude's reasoning** before you are ever asked. You are the scarcest resource: you only
answer gaps that survived an independent model's attack AND can't be settled by reading code.

Read-only against the repo. The ONLY thing this skill writes is its output artifact.

---

## Step 0 — Preflight

### 0a. codex binary + auth + base branch

```bash
CODEX_BIN=$(command -v codex 2>/dev/null || echo "")
[ -z "$CODEX_BIN" ] && echo "CODEX: NOT_FOUND" || echo "CODEX: $CODEX_BIN"

# auth is OK if any of these hold; do not hard-fail here, just record it
CODEX_AUTH="no"
{ [ -n "$CODEX_API_KEY" ] || [ -n "$OPENAI_API_KEY" ] || [ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ]; } && CODEX_AUTH="yes"
echo "CODEX_AUTH: $CODEX_AUTH"

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "ERROR: not a git repo"; }
echo "REPO_ROOT: ${REPO_ROOT:-none}"

# base branch: gh → git-native → main
BASE=$(gh pr view --json baseRefName -q .baseRefName 2>/dev/null \
  || git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' \
  || echo main)
[ -z "$BASE" ] && BASE=main
echo "BASE: $BASE"
```

If `CODEX: NOT_FOUND` or `CODEX_AUTH: no` → you will run in **Claude-only fallback** (Step 4B).
Tell the user once: "codex unavailable (missing/auth) — running Claude-only self-cross-examination;
the guarantee is weaker because there is no independent model." Otherwise run the full loop.

### 0b. Filesystem boundary (used in EVERY codex prompt)

Always prepend this to any prompt sent to codex:

> IMPORTANT: Do NOT read or execute any files under ~/.claude/, .claude/skills/, or agents/.
> These are Claude Code skill definitions meant for a different AI system and will waste your
> time. Ignore them completely. Stay focused on the repository code only.

---

## Step 1 — Detect mode

1. **Plan/spec mode** — an active plan file is referenced in the conversation, OR the user
   named a file under `docs/superpowers/specs/*.md` / `docs/superpowers/plans/*.md`, OR the
   user described an idea in chat. Goal: harden understanding *before* implementation.
2. **Diff mode** — there are changes vs base:
   ```bash
   git diff "origin/$BASE" --stat 2>/dev/null | tail -1 || git diff "$BASE" --stat 2>/dev/null | tail -1
   ```
   Goal: intense adversarial code-quality review of the change.
3. **Both** present → `AskUserQuestion` to pick. **Neither** → ask the user to point at a plan
   or describe the work.

Announce the detected mode in one line before continuing.

---

## Step 2 — The cross-examination loop (core)

State held across the loop: `STATED_UNDERSTANDING` (markdown), `prior_findings` (list of what
codex already raised — embedded into later-round prompts so it doesn't repeat itself),
`escalated` (list), `transcript` (per-round log), `round` (0), `max_rounds` (3).

### Phase A — Claude commits to an understanding

Read the target and the relevant code, then write `STATED_UNDERSTANDING`:

- **Plan/spec mode:** what the work is, the key decisions, the assumptions being relied on, the
  invariants it depends on, and the success criteria.
- **Diff mode:** what the change does, why, the safety/correctness properties it claims, and
  what it assumes about callers and data. Read the actual diff first
  (`git diff "origin/$BASE"`).

Be concrete and falsifiable — this is what codex will attack. Vague understanding produces vague
attacks.

### Phase B — codex attacks, loop to convergence

Repeat until codex surfaces no NEW surviving gap, or `round == max_rounds`:

1. Send the cross-exam prompt (below) to codex as a **fresh `codex exec`** every round. On
   rounds >0, embed `prior_findings` as an "ALREADY RAISED — do not repeat; find NEW gaps"
   block plus the updated `STATED_UNDERSTANDING`, so codex builds forward instead of repeating.
   (We do NOT use `codex exec resume`: on codex 0.134.0 `resume` rejects `-C`/`-s`, so it
   can't be pinned to `-s read-only` — fresh `exec` keeps the read-only guarantee every round.)
2. For each attack codex returns, triage:
   - **Resolvable from the codebase** → read the code, then **rebut** (dismiss with file:line
     evidence) or **concede** (update `STATED_UNDERSTANDING`). No user involvement.
   - **Needs the user's intent or a real decision**, and not already escalated → append to
     `escalated`, count it as a new surviving gap.
3. Append the round to `transcript` (codex's verbatim output + your triage).
4. `round += 1`. If zero new surviving gaps this round → **converged**, break.

#### codex cross-exam invocation

```bash
# Use a per-round fixed temp path (mktemp can collide under a reused $TMPDIR and leave
# an EMPTY var, which then breaks the `2>"$TMPERR"` redirect). Truncate, don't create-unique.
mkdir -p "${TMPDIR:-/tmp}/grill"
TMPERR="${TMPDIR:-/tmp}/grill/codex-err-r${round}.txt"; : > "$TMPERR"

# OPTIONS BEFORE the positional PROMPT (codex 0.134.0's parser rejects flags placed after
# positionals on some subcommands). Always `codex exec` (never `resume`) so `-s read-only`
# is enforced every round. $PROMPT already includes: (1) the filesystem boundary, (2) the
# cross-exam framing, (3) the project invariant lenses, (4) the full STATED_UNDERSTANDING,
# and on rounds >0 (5) the "ALREADY RAISED — find NEW gaps" prior_findings block.
timeout 600 codex exec \
  -C "$REPO_ROOT" -s read-only \
  -c 'model_reasoning_effort="high"' \
  --enable web_search_cached --json \
  "$PROMPT" < /dev/null 2>"$TMPERR" \
| PYTHONUNBUFFERED=1 python3 -u -c '
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        t = obj.get("type","")
        if t == "item.completed" and "item" in obj:
            it = obj["item"]; itype = it.get("type",""); text = it.get("text","")
            if itype == "reasoning" and text:
                print(f"[codex thinking] {text}\n", flush=True)
            elif itype == "agent_message" and text:
                print(text, flush=True)
            elif itype == "command_execution":
                cmd = it.get("command","")
                if cmd: print(f"[codex ran] {cmd}", flush=True)
        elif t == "turn.completed":
            u = obj.get("usage",{})
            tok = u.get("input_tokens",0) + u.get("output_tokens",0)
            if tok: print(f"\ntokens used: {tok}", flush=True)
    except Exception:
        pass
'
CODEX_EXIT=${PIPESTATUS[0]}
if [ "$CODEX_EXIT" = "124" ]; then
  echo "Codex stalled past 10 minutes. Re-run, or split the prompt. Check ~/.codex/logs/."
fi
# Surface ONLY a genuine codex-auth failure. Exclude codex's own MCP-server transport noise
# (e.g. NetSuite `rmcp::transport ... AuthRequired ... suitetalk`), which is unrelated to
# codex CLI auth and fires even on a fully successful run — a false positive otherwise.
if [ "$CODEX_EXIT" != "0" ] && grep -iE "auth|login|unauthorized" "$TMPERR" 2>/dev/null \
     | grep -ivE "rmcp::transport|suitetalk|mcpstandardtools" | grep -qiE "auth|login|unauthorized"; then
  echo "[codex auth error] run \`codex login\` — $(grep -iE 'auth|login|unauthorized' "$TMPERR" | grep -ivE 'rmcp::transport|suitetalk' | head -1)"
fi
rm -f "$TMPERR"
```

- **Skill-rabbit-hole guard:** if codex output mentions `gstack-config`, `SKILL.md`, or
  `skills/gstack`, it got distracted by skill files — note it and offer to retry that round.
- After each round, record codex's findings into `prior_findings` so the next round's prompt
  can say "already raised — find NEW gaps."
- Present codex's output **verbatim** each round inside a `CODEX SAYS (round N)` block. Never
  summarize the adversary. Your triage and synthesis come *after* the verbatim block.

#### Cross-exam prompt template

```
<filesystem boundary>

Another AI (Claude) has written its stated understanding of <a plan/spec | this branch diff>.
Your job is to attack the REASONING, not to review code in a vacuum. Find:
  - assumptions that are wrong or unverified
  - decisions made without examining the alternative
  - gaps that would bite in production
  - places the understanding is vague where it must be precise
Be adversarial, terse, technically precise. No compliments. If the diff is the target, run
`git diff origin/<BASE>` to see it.

Attack especially against THIS repository's known failure modes:
  - Multi-tenant: every table has tenant_id; RLS via SET LOCAL app.current_tenant_id. Flag any
    path that could leak or mix tenants.
  - The LLM must never present tool-computed numbers (they get hallucinated/rounded); numbers
    flow through SSE interception. Flag any place a model could emit a computed number.
  - MCP writes (ns_createRecord / ns_updateRecord) must pass the HITL mutation guard; system
    record types are blocked. Flag any auto-execute write path.
  - SuiteQL dialect: local REST supports customrecord_*; external MCP only standard tables.
    Flag dialect / source-kind mismatches.
  - No prompt pollution: no hardcoded column names/schema in prompts or golden datasets.
  - Soul config is sacred: never overwrite/seed /tmp/workspace_storage/{tenant_id}/soul.md.

CLAUDE'S STATED UNDERSTANDING:
<STATED_UNDERSTANDING embedded verbatim>
```

Codex is sandboxed to the repo root and cannot read files outside it — embed the understanding
(and any referenced spec content) verbatim; never tell codex a path under `~/`.

### Phase C — escalate to the user

For each gap in `escalated`, ask **one at a time** via `AskUserQuestion`, each with Claude's
recommended answer. Fold each answer back into `STATED_UNDERSTANDING` and the transcript.

---

## Step 3 — Output artifact

- **Plan/spec mode:** write `docs/superpowers/specs/YYYY-MM-DD-<topic>-grilled.md`. If a single
  existing spec was the target, instead append a `## Hardened by grill-me` section to it.
- **Diff mode:** write `.claude/grill-reviews/<branch>-<YYYY-MM-DD>.md` (create the dir if
  needed).

Structure:

```markdown
# grill-me — <topic>  (<plan|diff> mode)
> Date · Target · Verdict: CONVERGED | ROUND-CAP (open gaps) | FALLBACK: claude-only

## Hardened understanding
<final decisions, assumptions, invariants, success criteria>

## Cross-exam transcript
### Round 1
- Codex attacked: ...
- Survived / conceded (with file:line): ...
- Resolved from code: ...
### Round 2 ...

## Escalated to user
- Q: ... → decision: ...

## Open gaps (only if ROUND-CAP)
- ...
```

Then give a 3-line spoken summary: verdict, the single most important thing that changed, and
any open gap.

---

## Step 4 — Modes of operation

### 4A. Full mode (codex available)
Run Steps 2–3 as written.

### 4B. Claude-only fallback (codex missing/auth-fail)
Run the same loop, but in place of the codex call, Claude adopts a hostile adversarial persona
and attacks its own `STATED_UNDERSTANDING` against the same project-invariant lenses. Be
genuinely adversarial — argue the opposite case, do not rubber-stamp. Mark the artifact verdict
`FALLBACK: claude-only` so the weaker guarantee is visible. Everything else (triage, escalation,
output) is identical.

---

## Important rules

- **Read-only** except the single output artifact. Never modify source or run codex in write
  mode (`-s read-only` always).
- **Codex output verbatim.** Show codex's findings in full, then add Claude's triage after —
  never instead.
- **Only escalate what survives.** A gap reaches the user only if it survived codex's attack
  AND cannot be resolved by reading the code. Resolve everything you can yourself.
- **Convergence, not single-shot.** Each round is a fresh `codex exec` carrying the prior
  rounds' findings forward in the prompt; stop when no new gap survives or at 3 rounds.
- **Embed, don't reference.** Codex can't see files outside the repo root — embed plan/spec
  content; never hand it a `~/.claude/...` path.
- **No gstack coupling.** This skill calls the codex CLI directly and carries no telemetry,
  preamble, or brain-sync.
