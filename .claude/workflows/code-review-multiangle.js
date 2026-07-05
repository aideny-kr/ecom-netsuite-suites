export const meta = {
  name: 'code-review-multiangle',
  description: 'Generalized 8-angle find -> verify -> synthesize code review for any diff/PR/branch (the T2 review gate). 7 Claude angles + 1 INDEPENDENT-MODEL angle (codex, grill-me) so the gate is not Claude-on-Claude (which shares blind spots). Fails CLOSED: agent failures surface a top-level INCOMPLETE status, never a silent "0 findings".',
  whenToUse: 'Tier-T2 PRs (write-path / financial / HITL / auth / migration / review-tooling). args.target = PR number or branch/ref; args.base = optional base override; args.diff = raw unified diff to bypass resolution (TRUSTED caller-supplied — only for diffs you computed yourself).',
  phases: [
    { title: 'Prep', detail: 'resolve base ref + the unified diff (fail closed on empty)' },
    { title: 'Find', detail: '7 Claude finder angles + 1 codex (independent-model) angle in parallel; failed angles -> top-level INCOMPLETE' },
    { title: 'Verify', detail: 'one verifier per candidate (gets the per-file diff hunk); failure -> UNVERIFIED, not dropped' },
  ],
}

// ---- model-tiering harness — canonical: ~/.claude/workflows/model-tiering.md ----
const TIER = {
  plan:['fable','high'], architect:['fable','high'], design:['fable','high'], spec:['fable','high'],
  synthesize:['fable','high'], judge:['fable','high'], decide:['fable','high'],
  reason:['sonnet','medium'], verify:['sonnet','medium'], 'review-angle':['sonnet','medium'],
  analyze:['sonnet','medium'], implement:['sonnet','medium'],
  search:['haiku','low'], explore:['haiku','low'], read:['haiku','low'], map:['haiku','low'],
  format:['haiku','low'], diff:['haiku','low'], extract:['haiku','low'], mechanical:['haiku','low'], rename:['haiku','low'],
}
const EXPENSIVE = new Set(['fable','opus'])
function makeGate(limit){
  let active=0; const q=[]
  const pump=()=>{ while(active<limit && q.length){ active++
    const {fn,res,rej}=q.shift(); Promise.resolve().then(fn).then(res,rej).finally(()=>{active--;pump()}) } }
  return fn=>new Promise((res,rej)=>{ q.push({fn,res,rej}); pump() })
}
const _topGate = makeGate(3)                         // <=3 fable/opus agents at once
function tagent(role, prompt, opts={}){
  const [model,effort] = TIER[role] || ['sonnet','medium']
  const run = () => agent(prompt, { label: role, effort, ...opts, model: (opts.model||model) })
  return EXPENSIVE.has(opts.model||model) ? _topGate(run) : run()
}
// ---- end harness ----

const REPO_INVARIANTS = `
REPO-SPECIFIC INVARIANTS — flag any place the diff could violate one:
- Multi-tenant: every table has tenant_id; RLS via SET LOCAL app.current_tenant_id (set_tenant_context). Flag any query/path that could mix or leak tenants, or a mutation without the tenant filter.
- The LLM must NEVER present tool-computed numbers (hallucination/rounding) — numbers flow through SSE interception (_intercept_tool_result -> data_table/task_output). Flag any place a model could emit a computed number.
- MCP writes (ns_createRecord/ns_updateRecord) must pass the HITL mutation_guard; system record types blocked. Flag any auto-execute write path.
- No prompt pollution: no hardcoded column names/schema in prompts or golden datasets.
- Soul config is sacred: never overwrite/seed /tmp/workspace_storage/{tenant_id}/soul.md.
- SuiteQL dialect: local REST supports customrecord_*; external MCP only standard tables.
- Recon HITL: approve writes one audit row per line, never auto-posts to NetSuite, and a closed/locked run rejects approve (hard freeze).`

// ----- arg validation (fail-closed on hostile/huge input) ------------------
function asArg(v, name, max) {
  if (v == null) return null
  if (typeof v !== 'string') throw new Error(`args.${name} must be a string`)
  if (v.length > max) throw new Error(`args.${name} too long (${v.length} > ${max})`)
  return v
}
let targetSpec, baseArg, providedDiff
try {
  targetSpec = asArg(args && args.target, 'target', 200)
  baseArg = asArg(args && args.base, 'base', 200)
  providedDiff = asArg(args && args.diff, 'diff', 2_000_000)
  for (const [v, n] of [[targetSpec, 'target'], [baseArg, 'base']]) {
    if (v && /[\n\r]/.test(v)) throw new Error(`args.${n} must not contain newlines`)
  }
} catch (e) {
  return { status: 'INVALID_ARGS', error: String(e.message || e), findings: [] }
}

// -------------------------------------------------------------------- Prep
phase('Prep')
let diff = providedDiff
let baseUsed = baseArg
if (!diff) {
  const prep = await tagent('reason',
    `Resolve the unified diff under review. cwd = repo root. Use Bash.
Target: ${targetSpec ? `"${targetSpec}"` : 'the current branch HEAD'}.
Base override: ${baseUsed ? `"${baseUsed}"` : 'none — resolve the correct base yourself'}.
Steps:
1. Determine the BASE ref:
   - If target looks like a PR number (digits, optionally "#"-prefixed): get its base via curl + $(gh auth token) against api.github.com (gh CLI TLS may be broken here) -> .base.ref; fetch that base. Note if the head is a fork.
   - Else if a base override was given, use it.
   - Else resolve the default base: \`git symbolic-ref refs/remotes/origin/HEAD\` (strip refs/remotes/origin/); fall back to "main" ONLY if that fails.
2. Compute \`git diff <base>...<target-ref>\` (THREE-dot, merge-base based). For a PR, target-ref = the PR head.
3. FAIL CLOSED: if the resolved diff is EMPTY, do NOT fall back to \`git diff HEAD\` or any workspace diff. Set diff to the literal "EMPTY_DIFF".
4. Retry any git command that fails with a sandbox "Operation not permitted".
Return {"base": "<base you used>", "diff": "<raw unified diff, or EMPTY_DIFF>"}. The 'base' you report is surfaced to the human reviewer to sanity-check against the real PR base.`,
    {
      label: 'prep:diff', phase: 'Prep',
      schema: { type: 'object', additionalProperties: false, required: ['base', 'diff'], properties: { base: { type: 'string' }, diff: { type: 'string' } } },
    }
  )
  if (!prep) return { status: 'PREP_FAILED', error: 'could not resolve the diff (fail-closed; no review performed)', findings: [] }
  baseUsed = prep.base
  diff = prep.diff
}
if (!diff || diff.trim() === '' || diff.trim() === 'EMPTY_DIFF') {
  return { status: 'EMPTY_DIFF', base: baseUsed, error: 'nothing to review for this target (fail-closed; NOT substituting workspace state)', findings: [] }
}
const diffLines = diff.split('\n').length
log(`reviewing target=${targetSpec || 'HEAD'} base=${baseUsed || '?'} (${diffLines} diff lines) — verify 'base' matches the real PR base.`)
if (diffLines > 4000) log(`WARNING: large diff (${diffLines} lines) — finder recall may degrade; consider splitting by path.`)

// Per-file diff slice so a verifier sees DELETED lines for its candidate's file (Angle B
// deletion-regressions can't be verified from current-file-only).
function fileSlice(fullDiff, file) {
  if (!file) return ''
  const parts = fullDiff.split(/\n(?=diff --git )/)
  // EXACT match on the `diff --git a/<old> b/<new>` header paths only. No basename
  // fuzzy fallback (it could feed a verifier another file's hunk on basename collision).
  // No match -> '' -> the verifier prompt tells it to Read the file / `git diff -- <file>`.
  return parts.find(p => {
    const m = p.match(/^diff --git a\/(\S+) b\/(\S+)/)
    return !!m && (m[1] === file || m[2] === file)
  }) || ''
}

const FINDER_CTX = `You are reviewing a code change for RECALL (catch every real bug a careful reviewer would catch). cwd = repo root.
The unified diff under review is below. Read it, then Read the enclosing functions and related files for context
(Read/Grep only — do NOT modify anything). Cite file:line. Surface even uncertain candidates with a nameable failure scenario.
${REPO_INVARIANTS}

DIFF UNDER REVIEW:
${diff}`

const CAND_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['candidates'],
  properties: {
    candidates: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false, required: ['file', 'line', 'summary', 'failure_scenario', 'category'],
        properties: {
          file: { type: 'string' }, line: { type: 'string' }, summary: { type: 'string' },
          failure_scenario: { type: 'string' },
          category: { type: 'string', enum: ['correctness', 'reuse', 'simplification', 'efficiency', 'altitude'] },
        },
      },
    },
  },
}

const ANGLES = [
  { key: 'A-linebyline', prompt: 'ANGLE A - line-by-line (correctness). Every added/changed hunk line by line, then Read the enclosing function. inverted/wrong condition, off-by-one, null deref, missing await, falsy-zero check, wrong-variable copy-paste, swallowed error, SQL three-valued-logic, transaction/flush ordering. Up to 6.' },
  { key: 'B-removed', prompt: 'ANGLE B - removed-behavior auditor (correctness). For every DELETED/replaced line, name the invariant/guard/validation/error-path it enforced, then find where the new code re-establishes it. If you cannot, that is a candidate. Up to 6.' },
  { key: 'C-crossfile', prompt: 'ANGLE C - cross-file tracer (correctness). Grep callers and callees of each changed symbol; does the change break a call site (new precondition, changed return shape, new exception, ordering dependency)? Up to 6.' },
  { key: 'Reuse', prompt: 'ANGLE Reuse (cleanup). Does new code re-implement something the codebase already has? Grep shared/utility modules; name the existing helper. Up to 6.' },
  { key: 'Simplification', prompt: 'ANGLE Simplification (cleanup). Redundant/derivable state, copy-paste, deep nesting, dead code, a kept thin-wrapper that just forwards. Name the simpler form. Up to 6.' },
  { key: 'Efficiency', prompt: 'ANGLE Efficiency (cleanup). Redundant computation/IO, an extra round-trip that could fold in, sequential awaits that could batch, blocking work on a hot path. Up to 6.' },
  { key: 'Altitude', prompt: 'ANGLE Altitude. Is each change at the right depth or a fragile bandaid? Special cases on shared infra signal the fix is not deep enough. Up to 6.' },
]

// ----- Independent-model (codex) angle — DELEGATES to the grill-me skill ------------------
// The 7 angles above are all Claude subagents and SHARE Claude's blind spots (the very reason
// memory/feedback_independent_model_review_gate exists). This angle has a DIFFERENT model
// (codex) attack the same diff. The JS orchestration can't read files or invoke skills (it runs
// sandboxed), so the *subagent* this angle spawns is told to Read+follow .claude/skills/grill-me
// — that SKILL.md is the SINGLE SOURCE OF TRUTH for HOW to drive codex (flags, version quirks,
// auth-noise filtering, filesystem boundary). We keep ZERO copy of the codex recipe here; this
// prompt only carries the workflow-specific glue: which diff to review, the output contract, and
// the gate overrides (one pass, no user escalation, no artifact). If the skill file is absent or
// codex is unavailable it degrades to a hostile Claude-only persona + codex_used=false (mirrors
// grill-me's FALLBACK) — the gate never silently becomes Claude-only. The angle still "ok"s on
// fallback (no INCOMPLETE on codex-less hosts); it only fails the angle if the agent itself dies.
const CODEX_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['candidates', 'codex_used'],
  properties: { candidates: CAND_SCHEMA.properties.candidates, codex_used: { type: 'boolean' } },
}
const codexAnglePrompt = `ANGLE Codex — the INDEPENDENT-MODEL angle of this review. cwd = repo root; you have Bash + Read.
The other 7 angles are all Claude (shared blind spots); your job is to have a DIFFERENT model (codex) attack this diff.

SINGLE SOURCE OF TRUTH — do NOT reinvent how to drive codex. Read \`.claude/skills/grill-me/SKILL.md\` and follow its codex procedure verbatim: Step 0 (preflight — codex binary + auth + the filesystem boundary codex must obey) and Step 2's \`codex exec\` invocation (read-only, the exact flags/version quirks, the auth-noise filtering). That skill is the canonical definition of the codex mechanic; this angle only adapts its INPUT and OUTPUT. If \`.claude/skills/grill-me/SKILL.md\` does NOT exist on disk, go straight to FALLBACK.

SANDBOX NOTE: codex needs network (OpenAI) that the command sandbox blocks, so its call will fail sandboxed — retry that command with the sandbox disabled. Build the prompt file AND run codex in the SAME unsandboxed invocation: \`$TMPDIR\` differs across the sandbox boundary, so a file written in a sandboxed shell won't exist in the unsandboxed retry (you'd hand codex an empty prompt). Treat the presence of a codex agent_message / "tokens used" as the success signal — the captured shell exit code can be empty through codex's JSON pipe.

INPUT: review EXACTLY the diff below (the same change the other angles see — do not recompute it from git). Write it to "$TMPDIR/cr_diff.txt"; sanity-check it is ~${diffLines} lines (if your copy is truncated, FALLBACK rather than review partial input). Hand that diff to codex as the change under review, wrapped per the skill's filesystem-boundary + cross-exam framing.

OVERRIDES (this is an automated gate, not the interactive skill): run codex EXACTLY ONCE (no multi-round loop); do NOT escalate to the user (no AskUserQuestion) and do NOT write the skill's markdown artifact — a gap that would otherwise need the user is just reported as a finding here.

OUTPUT: turn each thing codex flagged into a candidate {file,line,summary,failure_scenario,category}; Read the cited code to set a precise line + concrete failure_scenario; do NOT invent findings codex did not raise (empty candidates is a valid result). Set codex_used=true. category is one of correctness|reuse|simplification|efficiency|altitude (codex findings are usually correctness). Return {"candidates":[...], "codex_used":true}.

FALLBACK (codex_used=false) — ONLY if the grill-me skill file is absent, OR codex is missing/unauthed/timed out: adopt a genuinely hostile adversarial persona and attack the diff yourself (argue the opposite case; do NOT rubber-stamp), ESPECIALLY against this repository's known failure modes (the skill carries these too, but a skill-absent fallback would not have read them):
${REPO_INVARIANTS}
Return {"candidates":[...], "codex_used":false}.

DIFF UNDER REVIEW:
${diff}`

// -------------------------------------------------------------------- Find
phase('Find')
const claudeFinders = ANGLES.map(a => () =>
  tagent('review-angle', FINDER_CTX + '\n\nTASK: ' + a.prompt, { label: `find:${a.key}`, phase: 'Find', schema: CAND_SCHEMA })
    .then(r => ({ key: a.key, ok: !!r, candidates: (r && r.candidates) || [] }))
    .catch(() => ({ key: a.key, ok: false, candidates: [] }))
)
// Carry codex_used out on the RETURNED result (not a closure side-effect) so the top-level
// metadata derives from data, never from parallel()'s execution order/retry semantics.
const codexFinder = () =>
  tagent('reason', codexAnglePrompt, { label: 'find:Codex(independent)', phase: 'Find', schema: CODEX_SCHEMA })
    .then(r => ({ key: 'Codex', ok: !!r, candidates: (r && r.candidates) || [], codex_used: !!(r && r.codex_used) }))
    .catch(() => ({ key: 'Codex', ok: false, candidates: [], codex_used: false }))
const finderRaw = await parallel([...claudeFinders, codexFinder])
const codexRes = finderRaw.find(x => x.key === 'Codex') || { ok: false, codex_used: false }
const codexUsed = codexRes.codex_used
log(`independent-model angle: ${codexRes.ok ? (codexUsed ? 'codex (real second model)' : 'FALLBACK claude-only — codex unavailable, weaker guarantee') : 'ANGLE FAILED -> INCOMPLETE'}`)
const failedAngles = finderRaw.filter(x => !x.ok).map(x => x.key)
if (failedAngles.length) log(`WARNING: ${failedAngles.length} finder angle(s) FAILED -> result.status will be INCOMPLETE: ${failedAngles.join(', ')}`)
const all = finderRaw.flatMap(x => x.candidates)

const seen = new Set(); const deduped = []
for (const c of all) {
  const key = `${c.file}|${c.line}|${(c.summary || '').toLowerCase().replace(/\s+/g, ' ').trim()}`
  if (seen.has(key)) continue
  seen.add(key); deduped.push(c)
}
log(`finders surfaced ${all.length} candidates -> ${deduped.length} after exact-dedup`)

// -------------------------------------------------------------------- Verify
phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['verdict', 'severity', 'reason'],
  properties: {
    verdict: { type: 'string', enum: ['CONFIRMED', 'PLAUSIBLE', 'REFUTED'] },
    severity: { type: 'string', enum: ['blocker', 'major', 'minor', 'nit'] },
    reason: { type: 'string' },
  },
}
// Failed/skipped verify -> UNVERIFIED at MAJOR (needs human; never under-rank an unverifiable finding).
const unverified = c => ({ ...c, verdict: 'UNVERIFIED', severity: 'major', reason: 'verifier agent failed/skipped — PRESERVED at major for human review (fail-closed)' })
const verifyGate = makeGate(6)   // <=6 concurrent verifiers (was up to ~16 -> the rate-limit burst)
const verified = await parallel(deduped.map(c => () => verifyGate(() => {
  const slice = fileSlice(diff, c.file)
  const ctx = `You are verifying ONE candidate from a code review. cwd = repo root. Re-read the cited file(s) yourself to confirm — do NOT trust the candidate. ${REPO_INVARIANTS}

This candidate's file diff hunk (so you can see DELETED lines too):
${slice || '(no matching hunk found — Read the file and, if needed, run `git diff <base>...HEAD -- ' + c.file + '`)'}

Return CONFIRMED (constructible bug shown), PLAUSIBLE (realistic reachable state, not provably impossible), or REFUTED (factually wrong / impossible by a type/constant/invariant you quote / already handled — cite the guard). Recall-biased: only REFUTE when you can construct the disproof. Assign severity (blocker/major/minor/nit).

CANDIDATE:
${JSON.stringify(c, null, 2)}`
  return tagent('verify', ctx, { label: `verify:${(c.file || '').split('/').pop()}:${c.line}`, phase: 'Verify', schema: VERIFY_SCHEMA })
    .then(v => (v ? { ...c, ...v } : unverified(c))).catch(() => unverified(c))
})))

// -------------------------------------------------------------------- Synthesize
const SEV = { blocker: 0, major: 1, minor: 2, nit: 3 }
const VORD = { CONFIRMED: 0, UNVERIFIED: 1, PLAUSIBLE: 2 }
const CORD = { correctness: 0, reuse: 1, simplification: 1, efficiency: 1, altitude: 1 }
const kept0 = verified.filter(Boolean).filter(v => v.verdict !== 'REFUTED')
kept0.sort((a, b) =>
  (SEV[a.severity] - SEV[b.severity]) ||
  ((VORD[a.verdict] ?? 1) - (VORD[b.verdict] ?? 1)) ||
  ((CORD[a.category] ?? 1) - (CORD[b.category] ?? 1))
)
const serious = kept0.filter(v => v.severity === 'blocker' || v.severity === 'major')
const tail = kept0.filter(v => v.severity === 'minor' || v.severity === 'nit')
const CAP = 15
const findings = serious.concat(tail).slice(0, Math.max(serious.length, CAP))
const truncatedLow = (serious.length + tail.length) - findings.length

return {
  // INCOMPLETE if any finder angle failed — a consumer must treat this as NOT a clean pass and re-run.
  status: failedAngles.length ? 'INCOMPLETE' : 'OK',
  target: targetSpec, base: baseUsed, diff_lines: diffLines,
  // codex_used=false means the independent-model angle fell back to Claude-only (codex
  // missing/unauthed) — the gate ran but WITHOUT a true second model; treat as a weaker pass.
  codex_used: codexUsed,
  failed_angles: failedAngles,
  total_candidates: all.length, deduped: deduped.length,
  confirmed: findings.filter(f => f.verdict === 'CONFIRMED').length,
  unverified: findings.filter(f => f.verdict === 'UNVERIFIED').length,
  kept: findings.length, truncated_low: truncatedLow,
  findings,
}
