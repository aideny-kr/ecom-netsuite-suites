export const meta = {
  name: 'build-with-review',
  description: 'TEMPLATE — implement a change across agents, then self-review by calling the code-review-multiangle gate (7 Claude angles + the independent-model codex/grill-me angle) as a FINAL phase. Advisory: it attaches findings and NEVER blocks the build. Copy this file and replace the Implement phase with your real build agents.',
  whenToUse: 'Authoring a build/implementation Workflow that should self-review before handing back, so the review fires automatically as part of the build instead of a separate manual run.',
  phases: [
    { title: 'Implement', detail: 'your build agents (placeholder in the template)' },
    { title: 'Diff', detail: 'an agent computes the unified diff the build produced' },
    { title: 'Review', detail: 'run code-review-multiangle inline on that diff — advisory, non-blocking' },
  ],
}

// ---------------------------------------------------------------- Implement
// REPLACE THIS PHASE with your real implementation agents, e.g.:
//   const built = await pipeline(tasks, t => agent(`implement ${t} (TDD: failing test FIRST)`, {...}))
// It is left empty in the template so the file is safe to run as a smoke of the Diff+Review wiring.
phase('Implement')
log('Implement: (template placeholder — replace with your real build agents)')

// ---------------------------------------------------------------- Diff
// The workflow SCRIPT cannot run git (it is sandboxed — only agent()/parallel()/log()/phase()/
// workflow()). So an agent computes the diff the build just produced. Tune the git command to
// match how your Implement phase leaves changes (uncommitted working tree vs committed to a branch).
phase('Diff')
const diffRes = await agent(
  `cwd = repo root; use Bash. Return the unified diff representing the change just built:
- If the build left UNCOMMITTED changes: \`git --no-pager diff HEAD\`.
- If it COMMITTED to the current branch: \`git --no-pager diff "$(git merge-base origin/HEAD HEAD)"...HEAD\`.
Use whichever is non-empty. If there is genuinely no change, return "EMPTY_DIFF".
Retry any git command that fails with a sandbox "Operation not permitted".
Return {"diff":"<raw unified diff or EMPTY_DIFF>"}.`,
  {
    label: 'compute-diff', phase: 'Diff',
    schema: { type: 'object', additionalProperties: false, required: ['diff'], properties: { diff: { type: 'string' } } },
  }
)
const builtDiff = (diffRes && diffRes.diff) || 'EMPTY_DIFF'

// ---------------------------------------------------------------- Review (advisory)
// Run the T2 gate INLINE on the built diff. workflow() nests one level only (a build-workflow ->
// the gate is fine; the gate is a leaf). The gate accepts args.diff (TRUSTED — we computed it
// ourselves, so it bypasses the gate's own diff-resolution prep) and returns
// {status, codex_used, findings, ...}. ADVISORY policy: attach the findings and DO NOT throw —
// the build is never blocked; a human triages review.findings. (To make it BLOCKING instead,
// throw when review.status === 'INCOMPLETE' or when a finding is blocker/major.)
phase('Review')
let review = { status: 'SKIPPED', findings: [] }
if (builtDiff.trim() && builtDiff.trim() !== 'EMPTY_DIFF') {
  review = await workflow('code-review-multiangle', { diff: builtDiff })
  const f = review.findings || []
  const serious = f.filter(x => x.severity === 'blocker' || x.severity === 'major')
  log(`Review: status=${review.status} codex_used=${review.codex_used} findings=${f.length} (serious=${serious.length}) — ADVISORY, not blocking`)
} else {
  log('Review: skipped — Diff phase produced no change')
}

// Hand findings back for the caller/human to triage. The build is NOT blocked.
return { review }
