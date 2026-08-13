import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

/**
 * The T2 gate decides WHICH DIFF gets reviewed from `args`. If that resolution goes
 * wrong the gate does not error — it falls back to "the current branch HEAD" and
 * returns `status: OK` with confident findings about a diff nobody asked about.
 * A green gate on the wrong diff is strictly worse than no gate: the PR body then
 * claims a review that never happened.
 *
 * This is not hypothetical. On 2026-08-11 it happened twice in a row
 * (runs wf_4e37835d, wf_4061fdb2) — 9.7M tokens, 120 agents, ~54 minutes — while the
 * PR actually under review (frontend-only #197) had ZERO of its files examined. Both
 * runs reported status:OK. The cause: `args` arrives JSON-ENCODED AS A STRING, and
 * `'{"target":"197"}'.target` is `undefined`.
 *
 * Passing an object from the caller does NOT fix it — the harness stringifies in
 * transit, which is why the second attempt failed identically to the first.
 */

const SRC = new URL('./code-review-multiangle.js', import.meta.url)

/** Extract the marked arg-resolution block and eval it standalone. Mirrors the
 *  extract-then-eval approach tiering.test.mjs uses for the pasted TIER literal. */
function loadResolveArgs() {
  const src = readFileSync(SRC, 'utf8')
  const m = src.match(/--- BEGIN arg-resolution[^\n]*\n([\s\S]*?)\n\/\/ --- END arg-resolution/)
  assert.ok(m, 'no marked arg-resolution block found in code-review-multiangle.js')
  return eval(`(() => { ${m[1]}; return resolveArgs })()`)
}

test('stringified args still resolve the target (the actual 2026-08-11 failure)', () => {
  const resolveArgs = loadResolveArgs()
  // This is exactly what the harness delivers.
  const out = resolveArgs('{"target": "197"}')
  assert.equal(out.target, '197',
    'args arrived as a JSON string and the target was lost -> the gate would silently ' +
    'review the current checkout instead of PR 197')
})

test('a real object still works (no regression for callers that get an object)', () => {
  const resolveArgs = loadResolveArgs()
  assert.equal(resolveArgs({ target: '197' }).target, '197')
})

test('base and diff survive the same coercion', () => {
  const resolveArgs = loadResolveArgs()
  const out = resolveArgs('{"target":"br","base":"origin/main","diff":"diff --git a/x b/x"}')
  assert.equal(out.base, 'origin/main')
  assert.equal(out.diff, 'diff --git a/x b/x')
})

test('unparseable args are REFUSED, never silently downgraded to the current checkout', () => {
  const resolveArgs = loadResolveArgs()
  // The caller asked for something specific. Reviewing something else and calling it
  // OK is the failure mode this whole file exists to prevent — so throw, and let the
  // workflow turn it into INVALID_ARGS.
  assert.throws(() => resolveArgs('not json at all'), /refus|could not be read|invalid/i)
})

test('no args at all is still allowed — reviewing the current branch is a legitimate use', () => {
  const resolveArgs = loadResolveArgs()
  // An empty-but-present object is the case the first version of this guard got
  // wrong: `Workflow({name: 'code-review-multiangle', args: {}})` threw INVALID_ARGS,
  // contradicting the code's own comment and breaking the documented default. Found
  // by running this gate against its own PR (#198), not by these tests.
  for (const empty of [undefined, null, '', {}, '{}']) {
    const out = resolveArgs(empty)
    assert.equal(out.target, null,
      `${JSON.stringify(empty)} asks for nothing, so "current branch HEAD" is correct — not INVALID_ARGS`)
  }
})

test('a request that asked for something unusable is still refused', () => {
  const resolveArgs = loadResolveArgs()
  // The distinction that makes the above safe: key PRESENCE means the caller wanted
  // something specific. If none of it yields a usable target, falling back to the
  // current checkout would be the original silent-wrong-review bug.
  assert.throws(() => resolveArgs({ targt: '198' }), /unrecognised|refus/i, "typo'd key")
  assert.throws(() => resolveArgs({ target: '' }), /refus|could not be read/i, 'empty target from an unset variable')
  assert.throws(() => resolveArgs('{"target": ""}'), /refus|could not be read/i, 'same, stringified')
})

test('a typo\'d target alongside a VALID base is refused, not silently downgraded', () => {
  // The hole the all-blank check left open, and the one that mattered most: every
  // invocation in this repo passes `base`, so a single typo'd target kept `base`
  // truthy, `!target && !base && !diff` stayed false, nothing threw, and the gate
  // reviewed the CURRENT CHECKOUT against origin/main while reporting OK — the exact
  // failure this function exists to prevent, alive inside its own fix. Found by
  // running the gate against its own PR (#198), not by the earlier tests.
  const resolveArgs = loadResolveArgs()
  assert.throws(() => resolveArgs({ targt: '198', base: 'origin/main' }), /unrecognised/i)
  assert.throws(() => resolveArgs('{"targt": "198", "base": "origin/main"}'), /unrecognised/i)
})

test('base alone is still legitimate — review the current branch against a given base', () => {
  // Must NOT be caught by the unknown-key rule: this is a real, supported invocation
  // and over-refusing would push callers back to passing nothing at all.
  const resolveArgs = loadResolveArgs()
  const out = resolveArgs({ base: 'origin/main' })
  assert.equal(out.base, 'origin/main')
  assert.equal(out.target, null)
})

test('a correctly-named but EMPTY key is refused even when another key is valid', () => {
  // Round 3's finding, and the one the unrecognised-key check could not catch: the key
  // name is right, the value is an unset variable. `base` being truthy kept the old
  // all-blank guard from firing, so the gate reviewed the current checkout.
  const resolveArgs = loadResolveArgs()
  assert.throws(() => resolveArgs({ target: '', base: 'origin/main' }), /empty|refus/i)
  assert.throws(() => resolveArgs('{"target": "", "base": "origin/main"}'), /empty|refus/i)
})

test('an explicitly empty diff is refused, NOT quietly passed through', () => {
  // I previously carved this out believing it would reach the EMPTY_DIFF status a few
  // lines below. It would not: that check is `if (!diff)`, and '' is falsy, so an
  // explicitly-empty diff fell into the agent git-resolution branch and reviewed
  // whatever happened to be checked out. The carve-out created the bug it was meant to
  // avoid. A caller holding an empty diff should not invoke the gate at all — which is
  // exactly what build-with-review.template.js already does with its non-empty guard.
  const resolveArgs = loadResolveArgs()
  assert.throws(() => resolveArgs({ diff: '' }), /empty|refus/i)
})

test('an array payload is refused — typeof [] is "object"', () => {
  // Round 4. `args: '[]'` parsed cleanly, carried no keys for either key-check to
  // catch, and returned "no target" — the silent fall-back class one more time, since
  // typeof [] === 'object' slipped past the non-object guard.
  const resolveArgs = loadResolveArgs()
  assert.throws(() => resolveArgs('[]'), /array/i)
  assert.throws(() => resolveArgs([]), /array/i)
})

test('the refusal says which problem it actually found', () => {
  // Round 4, both diagnostic-quality findings. These do not change WHETHER the gate
  // refuses — only whether the message sends a reader somewhere real. On a function
  // whose entire job is explaining a refusal, that is the function's job.
  const resolveArgs = loadResolveArgs()

  // `args: false` never reaches JSON.parse, so "could not be parsed" invented a JSON
  // syntax error to go looking for.
  assert.throws(() => resolveArgs(false), /must be an object/i)
  assert.throws(() => resolveArgs('nope'), /not valid JSON/i)

  // A typo'd key next to a wrong-typed value: report the typo, which is the cause,
  // not the type error, which is the symptom.
  assert.throws(() => resolveArgs({ targt: '198', base: 42 }), /unrecognised args: targt/i)
})

test('hostile input is still rejected (pre-existing guards survive the refactor)', () => {
  const resolveArgs = loadResolveArgs()
  assert.throws(() => resolveArgs({ target: 'a\nb' }), /newline/i, 'newline injection')
  assert.throws(() => resolveArgs({ target: 'x'.repeat(201) }), /too long/i, 'oversized target')
  assert.throws(() => resolveArgs({ target: 42 }), /must be a string/i, 'non-string target')
})
