export const meta = {
  name: 'code-review-multiangle',
  description: 'Generalized 7-angle find -> verify -> synthesize code review for any diff/PR/branch (the T2 review gate). Fails CLOSED: agent failures are surfaced, never silently dropped.',
  whenToUse: 'Tier-T2 PRs (write-path / financial / HITL / auth / migration / review-tooling). args.target = PR number or branch/ref; args.base = optional base override; args.diff = raw unified diff to bypass resolution.',
  phases: [
    { title: 'Prep', detail: 'resolve base ref + the unified diff (fail closed on empty)' },
    { title: 'Find', detail: '7 finder angles in parallel; failed angles are surfaced' },
    { title: 'Verify', detail: 'one verifier per candidate; verifier failure -> UNVERIFIED, not dropped' },
  ],
}

// This repo's known hazards — injected into finder AND verifier prompts so the
// "generalized" review still flags repo-specific failure modes.
const REPO_INVARIANTS = `
REPO-SPECIFIC INVARIANTS — flag any place the diff could violate one:
- Multi-tenant: every table has tenant_id; RLS via SET LOCAL app.current_tenant_id (set_tenant_context). Flag any query/path that could mix or leak tenants, or a mutation without the tenant filter.
- The LLM must NEVER present tool-computed numbers (hallucination/rounding) — numbers flow through SSE interception (_intercept_tool_result -> data_table/task_output). Flag any place a model could emit a computed number.
- MCP writes (ns_createRecord/ns_updateRecord) must pass the HITL mutation_guard; system record types blocked. Flag any auto-execute write path.
- No prompt pollution: no hardcoded column names/schema in prompts or golden datasets.
- Soul config is sacred: never overwrite/seed /tmp/workspace_storage/{tenant_id}/soul.md.
- SuiteQL dialect: local REST supports customrecord_*; external MCP only standard tables.
- Recon HITL: approve writes one audit row per line, never auto-posts to NetSuite, and a closed/locked run rejects approve (hard freeze).`

// -------------------------------------------------------------------- Prep
phase('Prep')
const targetSpec = args && args.target ? String(args.target) : null
const providedDiff = args && args.diff ? String(args.diff) : null
let diff = providedDiff
let baseUsed = args && args.base ? String(args.base) : null

if (!diff) {
  const prep = await agent(
    `Resolve the unified diff under review. cwd = repo root. Use Bash.
Target: ${targetSpec ? `"${targetSpec}"` : 'the current branch HEAD'}.
Base override: ${baseUsed ? `"${baseUsed}"` : 'none — resolve the correct base yourself'}.
Steps:
1. Determine the BASE ref:
   - If target looks like a PR number (digits, optionally "#"-prefixed): \`gh pr view <n> --json baseRefName -q .baseRefName\` (gh TLS may be broken here -> use \`curl\` + \`$(gh auth token)\` against api.github.com). Fetch that base. Note if it is a fork.
   - Else if a base override was given, use it.
   - Else resolve the default base: \`git symbolic-ref refs/remotes/origin/HEAD\` (strip refs/remotes/origin/) or \`gh repo view --json defaultBranchRef -q .defaultBranchRef.name\`; fall back to "main" ONLY if both fail.
2. Compute \`git diff <base>...<target-ref>\` (THREE-dot, merge-base based — robust to the base advancing). For a PR, target-ref is the PR head ref.
3. FAIL CLOSED: if the resolved diff is EMPTY, do NOT fall back to \`git diff HEAD\` or any workspace diff. Set diff to the literal "EMPTY_DIFF".
4. Retry any git command that fails with a sandbox "Operation not permitted".
Return {"base": "<base you used>", "diff": "<raw unified diff, or EMPTY_DIFF>"}.`,
    {
      label: 'prep:diff', phase: 'Prep',
      schema: { type: 'object', additionalProperties: false, required: ['base', 'diff'], properties: { base: { type: 'string' }, diff: { type: 'string' } } },
    }
  )
  if (!prep) return { target: targetSpec, error: 'PREP_FAILED: could not resolve the diff (fail-closed; no review performed)', findings: [] }
  baseUsed = prep.base
  diff = prep.diff
}
if (!diff || diff.trim() === '' || diff.trim() === 'EMPTY_DIFF') {
  return { target: targetSpec, base: baseUsed, error: 'EMPTY_DIFF: nothing to review for this target (fail-closed; NOT substituting workspace state)', findings: [] }
}
const diffLines = diff.split('\n').length
log(`reviewing target=${targetSpec || 'HEAD'} base=${baseUsed || '?'} (${diffLines} diff lines)`)
if (diffLines > 4000) log(`WARNING: large diff (${diffLines} lines) — finder recall may degrade; consider splitting the review by path.`)

const FINDER_CTX = `You are reviewing a code change for RECALL (catch every real bug a careful reviewer would catch). cwd = repo root.
The unified diff under review is below. Read it, then Read the enclosing functions and related files for context
(Read/Grep only — do NOT modify anything). Cite file:line. Surface even uncertain candidates with a nameable failure scenario.
${REPO_INVARIANTS}

DIFF UNDER REVIEW:
${diff}`

// Verifiers do NOT get the full diff (they Read the cited files) — keeps cost ~7x, not ~(7 + N candidates)x.
const VERIFY_CTX = `You are verifying ONE candidate finding from a code review. cwd = repo root.
Re-read the cited file(s) yourself to confirm — do NOT trust the candidate. ${REPO_INVARIANTS}`

const CAND_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['candidates'],
  properties: {
    candidates: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['file', 'line', 'summary', 'failure_scenario', 'category'],
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
  { key: 'A-linebyline', prompt: 'ANGLE A - line-by-line (correctness). Read every added/changed hunk line by line, then Read the enclosing function (bugs in unchanged lines of a touched function are in scope). inverted/wrong condition, off-by-one, null deref, missing await, falsy-zero check, wrong-variable copy-paste, swallowed error, SQL three-valued-logic, transaction/flush ordering. Up to 6.' },
  { key: 'B-removed', prompt: 'ANGLE B - removed-behavior auditor (correctness). For every DELETED/replaced line, name the invariant/guard/validation/error-path it enforced, then find where the new code re-establishes it. If you cannot, that is a candidate. Up to 6.' },
  { key: 'C-crossfile', prompt: 'ANGLE C - cross-file tracer (correctness). Grep callers and callees of each changed symbol. Does the change break a call site (new precondition, changed return shape, new exception, ordering dependency)? Up to 6.' },
  { key: 'Reuse', prompt: 'ANGLE Reuse (cleanup). Does the new code re-implement something the codebase already has? Grep shared/utility modules; name the existing helper. Up to 6.' },
  { key: 'Simplification', prompt: 'ANGLE Simplification (cleanup). Redundant/derivable state, copy-paste, deep nesting, dead code, a kept thin-wrapper that just forwards. Name the simpler form. Up to 6.' },
  { key: 'Efficiency', prompt: 'ANGLE Efficiency (cleanup). Redundant computation/IO, an extra round-trip that could fold in, sequential awaits that could batch, blocking work on a hot path. Name the cheaper alternative. Up to 6.' },
  { key: 'Altitude', prompt: 'ANGLE Altitude. Is each change at the right depth or a fragile bandaid? Special cases on shared infra signal the fix is not deep enough. Up to 6.' },
]

// -------------------------------------------------------------------- Find
phase('Find')
const finderRaw = await parallel(ANGLES.map(a => () =>
  agent(FINDER_CTX + '\n\nTASK: ' + a.prompt, { label: `find:${a.key}`, phase: 'Find', schema: CAND_SCHEMA })
    .then(r => ({ key: a.key, ok: !!r, candidates: (r && r.candidates) || [] }))
    .catch(() => ({ key: a.key, ok: false, candidates: [] }))
))
const failedAngles = finderRaw.filter(x => !x.ok).map(x => x.key)
if (failedAngles.length) log(`WARNING: ${failedAngles.length} finder angle(s) FAILED (not "0 findings"): ${failedAngles.join(', ')}`)
const all = finderRaw.flatMap(x => x.candidates)

// Dedup ONLY true duplicates (same file+line+normalized summary) so distinct failure modes
// on the same line survive.
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
const unverified = c => ({ ...c, verdict: 'UNVERIFIED', severity: c.category === 'correctness' ? 'major' : 'minor', reason: 'verifier agent failed/skipped — PRESERVED for human review (fail-closed)' })
const verified = await parallel(deduped.map(c => () =>
  agent(
    VERIFY_CTX + `\n\nReturn CONFIRMED (constructible bug shown), PLAUSIBLE (realistic reachable state, not provably impossible), or REFUTED (factually wrong / impossible by a type/constant/invariant you quote / already handled — cite the guard). Recall-biased: only REFUTE when you can construct the disproof. Assign severity (blocker/major/minor/nit).\n\nCANDIDATE:\n${JSON.stringify(c, null, 2)}`,
    { label: `verify:${(c.file || '').split('/').pop()}:${c.line}`, phase: 'Verify', schema: VERIFY_SCHEMA }
  ).then(v => (v ? { ...c, ...v } : unverified(c))).catch(() => unverified(c))
))

// -------------------------------------------------------------------- Synthesize
const SEV = { blocker: 0, major: 1, minor: 2, nit: 3 }
const VORD = { CONFIRMED: 0, UNVERIFIED: 1, PLAUSIBLE: 2 } // unverified ranks above plausible: unknown, treat seriously
const CORD = { correctness: 0, reuse: 1, simplification: 1, efficiency: 1, altitude: 1 }
const kept0 = verified.filter(Boolean).filter(v => v.verdict !== 'REFUTED')
kept0.sort((a, b) =>
  (SEV[a.severity] - SEV[b.severity]) ||
  ((VORD[a.verdict] ?? 1) - (VORD[b.verdict] ?? 1)) ||
  ((CORD[a.category] ?? 1) - (CORD[b.category] ?? 1))
)
// NEVER drop a blocker/major. Cap only the minor/nit tail.
const serious = kept0.filter(v => v.severity === 'blocker' || v.severity === 'major')
const tail = kept0.filter(v => v.severity === 'minor' || v.severity === 'nit')
const CAP = 15
const findings = serious.concat(tail).slice(0, Math.max(serious.length, CAP))
const truncatedMinor = (serious.length + tail.length) - findings.length

return {
  target: targetSpec, base: baseUsed, diff_lines: diffLines,
  failed_angles: failedAngles,                 // non-empty => NOT a clean pass
  total_candidates: all.length, deduped: deduped.length,
  confirmed: findings.filter(f => f.verdict === 'CONFIRMED').length,
  unverified: findings.filter(f => f.verdict === 'UNVERIFIED').length,
  kept: findings.length, truncated_minor: truncatedMinor,
  findings,
}
