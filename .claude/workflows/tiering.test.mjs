import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = f => readFileSync(new URL(f, import.meta.url), 'utf8')

test('code-review-multiangle uses the tiering harness (no bare Opus fan-out)', () => {
  const s = read('./code-review-multiangle.js')
  assert.match(s, /const TIER = \{/, 'harness block missing')
  assert.match(s, /function tagent\(/, 'tagent() missing')
  assert.match(s, /tagent\('review-angle'/, 'finders not tiered to review-angle')
  assert.match(s, /tagent\('verify'/, 'verifiers not tiered to verify')
  assert.match(s, /tagent\('reason'/, 'prep/codex driver not tiered to reason')
  assert.match(s, /makeGate\(6\)/, 'verify burst not capped at 6')
  assert.doesNotMatch(s, /\bawait agent\(/, 'a bare `await agent(` remains — convert it to tagent')
})
