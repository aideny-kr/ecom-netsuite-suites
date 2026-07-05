import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = f => readFileSync(new URL(f, import.meta.url), 'utf8')

// The canonical model-tiering policy. MUST match ~/.claude/workflows/lib/model-tier.mjs
// (the tested source of truth) and every pasted copy. The global lib module lives outside
// the repo — it can't be imported here or in CI — so this snapshot is the in-repo drift
// guard for the two pasted workflow copies; lib/model-tier.test.mjs separately guards the
// module<->doc copy.
const EXPECTED_TIER = {
  plan: ['fable', 'high'], architect: ['fable', 'high'], design: ['fable', 'high'], spec: ['fable', 'high'],
  synthesize: ['fable', 'high'], judge: ['fable', 'high'], decide: ['fable', 'high'],
  reason: ['sonnet', 'medium'], verify: ['sonnet', 'medium'], 'review-angle': ['sonnet', 'medium'],
  analyze: ['sonnet', 'medium'], implement: ['sonnet', 'medium'],
  search: ['haiku', 'low'], explore: ['haiku', 'low'], read: ['haiku', 'low'], map: ['haiku', 'low'],
  format: ['haiku', 'low'], diff: ['haiku', 'low'], extract: ['haiku', 'low'], mechanical: ['haiku', 'low'], rename: ['haiku', 'low'],
}

// Extract the pasted `const TIER = {…}` object literal from a workflow script (no nested
// braces — values are arrays — so a lazy match to the first `}` is safe).
function extractTier(src) {
  const m = src.match(/const TIER = (\{[\s\S]*?\})\s*\n/)
  assert.ok(m, 'no `const TIER = {…}` block found')
  return eval('(' + m[1] + ')')
}

// Count raw (un-tiered) `agent(` calls. `tagent(` does NOT match `\bagent\(` (the `t` kills
// the word boundary), and line comments are stripped first so prose like `// only agent()/…`
// isn't counted. Every real fan-out MUST go through `tagent()`; the ONLY legitimate raw
// `agent(` is the single `() => agent(...)` inside the `tagent()` wrapper itself — so the
// count must be exactly 1. This catches the shapes a `/await agent(/` check misses:
// `() => agent(`, `return agent(`, `parallel([() => agent(...)])` — i.e. the exact
// un-tiered fan-out shapes that caused the original rate-limit burst.
function rawAgentCount(src) {
  const code = src.replace(/\/\/.*$/gm, '')
  return (code.match(/\bagent\(/g) || []).length
}

test('code-review-multiangle: tiered harness, no un-tiered agent() fan-out', () => {
  const s = read('./code-review-multiangle.js')
  assert.match(s, /const TIER = \{/, 'harness block missing')
  assert.match(s, /function tagent\(/, 'tagent() missing')
  assert.match(s, /tagent\('review-angle'/, 'finders not tiered to review-angle')
  assert.match(s, /tagent\('verify'/, 'verifiers not tiered to verify')
  assert.match(s, /tagent\('reason'/, 'prep/codex driver not tiered to reason')
  assert.match(s, /makeGate\(6\)/, 'verify burst not capped at 6')
  assert.equal(rawAgentCount(s), 1, 'exactly one raw agent( allowed (the tagent() internal); an un-tiered agent() fan-out was reintroduced')
})

test('build-with-review template: tiered harness, no un-tiered agent() fan-out', () => {
  const s = read('./build-with-review.template.js')
  assert.match(s, /const TIER = \{/, 'harness block missing')
  assert.match(s, /tagent\('diff'/, 'compute-diff not tiered to diff')
  assert.equal(rawAgentCount(s), 1, 'exactly one raw agent( allowed (the tagent() internal); an un-tiered agent() fan-out was reintroduced')
})

test('pasted TIER copies match the canonical policy (no drift)', () => {
  assert.deepEqual(extractTier(read('./code-review-multiangle.js')), EXPECTED_TIER, 'code-review-multiangle.js TIER drifted from canonical')
  assert.deepEqual(extractTier(read('./build-with-review.template.js')), EXPECTED_TIER, 'build-with-review.template.js TIER drifted from canonical')
})
