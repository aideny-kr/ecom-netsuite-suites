export const meta = {
  name: 'code-review-multiangle',
  description: 'Generalized 7-angle find -> verify -> synthesize code review for any diff/PR/branch (the T2 review gate)',
  whenToUse: 'Behavior-changing / write-path / financial / HITL / auth / migration PRs (tier T2). Pass args.target = a PR number or branch/ref; or args.diff = raw unified diff text.',
  phases: [
    { title: 'Prep', detail: 'resolve the unified diff under review' },
    { title: 'Find', detail: '7 finder angles in parallel, up to 6 candidates each' },
    { title: 'Verify', detail: 'one verifier per deduped candidate' },
  ],
}

// ---- Phase 0: resolve the diff -------------------------------------------------
phase('Prep')
const targetSpec = args && args.target ? String(args.target) : 'HEAD'
let diff = args && args.diff ? String(args.diff) : null
if (!diff) {
  diff = await agent(
    `Produce the unified diff under review for target "${targetSpec}". cwd = repo root.
- If the target is a PR number (digits, possibly "#"-prefixed), fetch that PR head and diff it against its base branch.
- Otherwise treat it as a branch/ref: \`git fetch origin main\`, then \`git diff origin/main...<ref>\` (three-dot, changes on the ref since the merge-base). If that is empty, fall back to \`git diff HEAD\`.
- If a git command fails with a sandbox "Operation not permitted", retry it.
Output ONLY the raw unified diff as your final message — no commentary, no fences.`,
    { label: `prep:diff:${targetSpec}`, phase: 'Prep' }
  )
}
if (!diff || diff.trim().length === 0) {
  return { target: targetSpec, error: 'empty diff — nothing to review', findings: [] }
}
log(`reviewing target=${targetSpec} (${diff.split('\n').length} diff lines)`)

const CTX = `You are reviewing a code change for RECALL (catch every real bug a careful reviewer would catch). cwd = repo root.
The unified diff under review is below. Read it, then Read the enclosing functions and related files for context
(Read/Grep only — do NOT modify anything). Cite file:line. Surface even uncertain candidates that have a nameable
failure scenario; the verify pass filters them.

DIFF UNDER REVIEW:
${diff}`

const CAND_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['candidates'],
  properties: {
    candidates: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['file', 'line', 'summary', 'failure_scenario', 'category'],
        properties: {
          file: { type: 'string' },
          line: { type: 'string' },
          summary: { type: 'string' },
          failure_scenario: { type: 'string' },
          category: { type: 'string', enum: ['correctness', 'reuse', 'simplification', 'efficiency', 'altitude'] },
        },
      },
    },
  },
}

const ANGLES = [
  { key: 'A-linebyline', prompt: 'ANGLE A - line-by-line (correctness). Read every added/changed hunk line by line, then Read the enclosing function (bugs in unchanged lines of a touched function are in scope). For each line: what input/state/timing/ordering makes it wrong? inverted/wrong condition, off-by-one, null deref, missing await, falsy-zero check, wrong-variable copy-paste, swallowed error, SQL three-valued-logic, transaction/flush ordering. Up to 6 candidates.' },
  { key: 'B-removed', prompt: 'ANGLE B - removed-behavior auditor (correctness). For every line the diff DELETES or replaces, name the invariant/guard/validation/error-path it enforced, then find where the new code re-establishes it. If you cannot, that is a candidate. Up to 6.' },
  { key: 'C-crossfile', prompt: 'ANGLE C - cross-file tracer (correctness). For each changed function/symbol, Grep its callers and callees. Does the change break a call site (new precondition, changed return shape, new exception, timing/ordering dependency)? Does a parallel change make a call unsafe? Up to 6.' },
  { key: 'Reuse', prompt: 'ANGLE Reuse (cleanup). Does the new code re-implement something the codebase already has? Grep shared/utility modules and files adjacent to the change; name the existing helper to call instead. Up to 6.' },
  { key: 'Simplification', prompt: 'ANGLE Simplification (cleanup). Flag unnecessary complexity the diff adds: redundant/derivable state, copy-paste with slight variation, deep nesting, dead code left behind, a kept thin-wrapper that just forwards. Name the simpler form. Up to 6.' },
  { key: 'Efficiency', prompt: 'ANGLE Efficiency (cleanup). Flag wasted work the diff introduces: redundant computation or repeated I/O, an extra round-trip that could be folded in, sequential awaits that could batch, blocking work on a hot path. Name the cheaper alternative. Up to 6.' },
  { key: 'Altitude', prompt: 'ANGLE Altitude. Is each change at the right depth or a fragile bandaid? Special cases layered on shared infrastructure signal the fix is not deep enough. Prefer generalizing the underlying mechanism. Up to 6.' },
]

phase('Find')
const finderResults = await parallel(
  ANGLES.map(a => () => agent(CTX + '\n\nTASK: ' + a.prompt, { label: `find:${a.key}`, phase: 'Find', schema: CAND_SCHEMA }))
)
const all = finderResults.filter(Boolean).flatMap(r => (r && r.candidates) || [])
const seen = new Set()
const deduped = []
for (const c of all) {
  const key = `${c.file}:${c.line}:${(c.summary || '').toLowerCase().replace(/[^a-z0-9 ]/g, '').split(' ').slice(0, 5).join(' ')}`
  if (seen.has(key)) continue
  seen.add(key); deduped.push(c)
}
log(`finders surfaced ${all.length} candidates -> ${deduped.length} after dedup`)

phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['verdict', 'severity', 'reason'],
  properties: {
    verdict: { type: 'string', enum: ['CONFIRMED', 'PLAUSIBLE', 'REFUTED'] },
    severity: { type: 'string', enum: ['blocker', 'major', 'minor', 'nit'] },
    reason: { type: 'string' },
  },
}
const verified = await parallel(deduped.map(c => () =>
  agent(
    CTX + `\n\nTASK: Verify ONE candidate. Re-read the diff + cited file(s) yourself; do not trust the candidate.
Return CONFIRMED (constructible bug shown), PLAUSIBLE (realistic reachable state, not provably impossible), or
REFUTED (factually wrong / impossible by a type/constant/invariant you quote / already handled in this diff — cite the guard).
Recall-biased: only REFUTE when you can construct the disproof. Assign severity (blocker/major/minor/nit).

CANDIDATE:
${JSON.stringify(c, null, 2)}`,
    { label: `verify:${(c.file || '').split('/').pop()}:${c.line}`, phase: 'Verify', schema: VERIFY_SCHEMA }
  ).then(v => ({ ...c, ...v })).catch(() => null)
))

const SEV = { blocker: 0, major: 1, minor: 2, nit: 3 }
const kept = verified.filter(Boolean)
  .filter(v => v.verdict === 'CONFIRMED' || v.verdict === 'PLAUSIBLE')
  .sort((a, b) => (SEV[a.severity] - SEV[b.severity]))
  .slice(0, 10)

return { target: targetSpec, total_candidates: all.length, deduped: deduped.length, kept_count: kept.length, findings: kept }
