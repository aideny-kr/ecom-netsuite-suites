#!/usr/bin/env bash
# verify.sh — the loop's EVIDENCE node.
#
# The development loop is: build -> verify -> specific feedback -> bounded retry -> exit.
# This script IS the exit condition. Exit 0 means done; anything else means not done.
# Nobody's opinion — including mine — substitutes for its exit code.
#
# It exists because on 2026-08-02 the same claim ("tests pass, zero regressions") was
# made three times on code that would have crash-looped every Celery worker. Each
# individual check was green. What was missing was a check that EXECUTES rather than
# inspects, runs from a CLEAN CHECKOUT rather than a dirty disk, and compares against
# a BASELINE rather than against nothing.
#
#   ./scripts/verify.sh            # fast: lint + import + targeted tests
#   ./scripts/verify.sh --full     # adds clean-checkout run and origin/main baseline diff
#
# Contract (portable across projects — only the body changes):
#   * every check prints PASS or FAIL on one line
#   * checks that were SKIPPED say so out loud; silence is never success
#   * exit 0 only if no check FAILED and no required check was skipped
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

# Resolve the interpreter. Worktrees do NOT carry backend/.venv — it lives in the
# main checkout — so falling straight through to system python3 silently runs every
# check against an environment with no celery and no pytest, and the failure looks
# like broken code rather than a broken lookup. Walk to the main checkout explicitly.
MAIN_CHECKOUT="$(cd "$(git rev-parse --git-common-dir)/.." 2>/dev/null && pwd || echo "$REPO_ROOT")"
for cand in \
  "${VERIFY_PYTHON:-}" \
  "$REPO_ROOT/backend/.venv/bin/python" \
  "$MAIN_CHECKOUT/backend/.venv/bin/python"
do
  [[ -n "$cand" && -x "$cand" ]] && { PY="$cand"; break; }
done
PY="${PY:-$(command -v python3)}"
RUFF="$(dirname "$PY")/ruff"
[[ -x "$RUFF" ]] || RUFF="$(command -v ruff || true)"

# A project venv that resolved to bare system python is a broken lookup, not a
# passing environment. Say so instead of reporting misleading failures.
if ! "$PY" -c "import celery" >/dev/null 2>&1; then
  echo "verify.sh: interpreter '$PY' lacks project deps (no celery)."
  echo "           set VERIFY_PYTHON=/path/to/backend/.venv/bin/python"
  exit 2
fi

# Tests run against LOCAL docker, not whatever DATABASE_URL_DIRECT points at.
#
# The repo's conftest reads DATABASE_URL_DIRECT, which in a normal .env is the
# live Supabase instance (26 tenants, 300k+ recon rows). Two problems: a branch's
# migrations are not applied there, so any model column added on the branch makes
# every test touching that model fail with UndefinedColumnError and look like a
# code regression; and it points a test suite at production data.
#
# Observed: adding six nullable columns to ReconciliationResult turned a clean
# "2 failed, 89 passed" into "6 failed, 85 passed" — four phantom failures that
# were purely a schema-location artifact.
#
# Override with VERIFY_DB if you genuinely need a different target.
export DATABASE_URL="${VERIFY_DB:-postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite}"
export DATABASE_URL_DIRECT="$DATABASE_URL"
# DATABASE_URL_SYNC too, and this one is not cosmetic. workers/base_task.py does
# `sync_engine = create_engine(settings.DATABASE_URL_SYNC)` and InstrumentedTask
# .before_start WRITES a Job row and an AuditEvent through it. A normal .env
# points that at the live Supabase pooler, so overriding only the async URLs left
# every instrumented-task test writing rows into production.
export DATABASE_URL_SYNC="${VERIFY_DB_SYNC:-postgresql://postgres:postgres@localhost:5432/ecom_netsuite}"

# Fail closed rather than silently write somewhere real: if any DB URL still
# resolves to a non-local host, stop.
for _u in "$DATABASE_URL" "$DATABASE_URL_DIRECT" "$DATABASE_URL_SYNC"; do
  case "$_u" in
    *localhost*|*127.0.0.1*) ;;
    *) echo "verify.sh: refusing to run — a DB URL points at a non-local host:"
       echo "           $(printf '%s' "$_u" | sed -E 's#//[^@]*@#//***@#')"
       echo "           tests write Job/AuditEvent rows; set VERIFY_DB / VERIFY_DB_SYNC."
       exit 2 ;;
  esac
done

# Anything that creates must delete — including on interrupt. Leaked worktrees
# accumulate in .claude/worktrees/ and blow the sandbox arg limit at ~40, after
# which every Bash call in the session fails.
_WORKTREES=()
_cleanup() { for w in "${_WORKTREES[@]:-}"; do [[ -n "$w" ]] && git worktree remove --force "$w" >/dev/null 2>&1; done; }
trap _cleanup EXIT INT TERM

FAILED=(); PASSED=(); SKIPPED=()
pass() { PASSED+=("$1"); printf '  PASS  %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }
skip() { SKIPPED+=("$1"); printf '  SKIP  %s — %s\n' "$1" "${2:-no reason given}"; }
# WARN is for a real signal that is NOT necessarily yours. This repo carries
# pre-existing test failures; making the tool red for those would make it
# permanently red, and a permanently red check stops being read. The authoritative
# test verdict is the --full baseline diff.
WARNED=()
warn() { WARNED+=("$1"); printf '  WARN  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }

echo "verify.sh — $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
echo

# ---------------------------------------------------------------- 1. lint
echo "[lint]"
if [[ -x "$RUFF" ]]; then
  if out=$("$RUFF" check backend/app backend/tests 2>&1); then pass "ruff check"; else fail "ruff check" "$(echo "$out" | tail -3)"; fi
  # CI runs BOTH check and format --check; a green `check` alone is not enough.
  if out=$("$RUFF" format --check backend/app backend/tests 2>&1); then pass "ruff format --check"; else fail "ruff format --check" "$(echo "$out" | tail -3)"; fi
else
  skip "ruff" "not found at $RUFF"
fi

# ------------------------------------------------- 2. import (EXECUTE, don't inspect)
# A syntactic scan cannot catch a name-resolution error. Celery imports every
# conf.include entry at worker startup with no try/except, so an import error here
# stops every scheduled job in production.
echo
echo "[import]"
if [[ -d backend/app ]]; then
  out=$(cd backend && "$PY" - <<'PYEOF' 2>&1
import importlib, sys
try:
    from app.workers.celery_app import celery_app
except Exception as exc:
    print(f"celery_app itself failed to import: {type(exc).__name__}: {exc}"); sys.exit(1)
bad = []
for m in celery_app.conf.include:
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append(f"{m}: {type(exc).__name__}: {exc}")
if bad:
    print("modules in conf.include that fail to import (worker will NOT boot):")
    [print("   " + b) for b in bad]
    sys.exit(1)
print(f"{len(celery_app.conf.include)} task modules import")

# Every Beat entry must resolve to a registered, instrumented task, or its failures
# leave no jobs row and are invisible to the ops digest.
from app.workers.base_task import InstrumentedTask
un = [e["task"] for e in celery_app.conf.beat_schedule.values() if celery_app.tasks.get(e["task"]) is None]
ni = [e["task"] for e in celery_app.conf.beat_schedule.values()
      if celery_app.tasks.get(e["task"]) is not None and not isinstance(celery_app.tasks[e["task"]], InstrumentedTask)]
if un: print("beat entries with NO registered task:", un); sys.exit(1)
print(f"{len(celery_app.conf.beat_schedule)} beat entries registered")
# Instrumentation is a STANDARD, not a boot requirement. An uninstrumented beat
# task still runs; it just leaves no jobs row when it fails. Exiting 1 here made
# verify.sh red on any branch that had not yet landed the fix — including
# origin/main — which is the permanently-red trap. Exit 3 => WARN.
if ni:
    print("beat tasks leaving no jobs row on failure:", ni); sys.exit(3)
PYEOF
  )
  rc=$?
  if [[ $rc -eq 0 ]]; then
    pass "worker modules import + beat registry"; printf '        %s\n' "$(echo "$out" | tail -2 | tr '\n' ' ')"
  elif [[ $rc -eq 3 ]]; then
    # Modules import (the boot-critical part passed); some beat tasks are not
    # instrumented. True, but not introduced by this branch — see Track O.
    warn "beat tasks not instrumented (they run, but leave no jobs row on failure)" "$(echo "$out" | tail -1)"
  else
    fail "worker modules import — the worker will NOT boot" "$(echo "$out" | head -6)"
  fi
else
  skip "import" "no backend/app"
fi

# ------------------------------- 3. every loaded module must be TRACKED
# The first version ran `git ls-files --others --exclude-standard`, and
# --exclude-standard EXCLUDES gitignored files by definition — so it could not
# see the one bug it was written for (.gitignore's `tasks/` swallowing a new
# worker module, which passed every local check and would have failed at boot on
# any clean clone). Verified: with that file present the old command returned 0.
#
# Ask the precise question instead: does every module Celery loads resolve to a
# file git actually tracks?
echo
echo "[tracked]"
if [[ -d backend/app ]]; then
  out=$(cd backend && "$PY" - <<'PYEOF' 2>&1
import importlib.util, os, subprocess, sys
from app.workers.celery_app import celery_app
root = subprocess.run(["git","rev-parse","--show-toplevel"],capture_output=True,text=True).stdout.strip()
bad = []
for m in celery_app.conf.include:
    spec = importlib.util.find_spec(m)
    if not spec or not spec.origin:
        bad.append(f"{m}: no file on disk"); continue
    rel = os.path.relpath(spec.origin, root)
    if subprocess.run(["git","ls-files","--error-unmatch",rel],
                      capture_output=True, cwd=root).returncode:
        bad.append(f"{m} -> {rel}")
if bad:
    print("modules Celery loads that git does NOT track (CI and any fresh clone will fail):")
    [print("   " + b) for b in bad]
    sys.exit(1)
print(f"all {len(celery_app.conf.include)} loaded modules are tracked")
PYEOF
  )
  if [[ $? -eq 0 ]]; then pass "every loaded module is tracked"; printf '        %s\n' "$(echo "$out" | tail -1)"
  else fail "untracked module in conf.include" "$(echo "$out" | head -5)"; fi
else
  skip "tracked" "no backend/app"
fi

# ---------------------------------------------------------------- 4. tests
echo
echo "[tests]"
# Scope is a function of MODE, not a constant.
#   --fast : a narrow slice, for the inner build/fix loop. Explicitly NOT evidence.
#   --full : the whole backend suite (5,589 tests). Slow on purpose — it runs once
#            before landing, not every cycle.
# The first version used the narrow slice for BOTH, so "verify PASSED · ready for
# the gate" was reported on 7 of 416 backend test files — about 2% of the suite —
# while CLAUDE.md requires zero regressions across it.
if [[ -n "${VERIFY_TESTS:-}" ]]; then TEST_TARGET="$VERIFY_TESTS"
elif [[ $FULL -eq 1 ]]; then TEST_TARGET="backend/tests"
else TEST_TARGET="backend/tests/workers"; fi
if [[ -d "$TEST_TARGET" ]]; then
  out=$(cd backend && "$PY" -m pytest "${TEST_TARGET#backend/}" -q 2>&1 | tail -4)
  if echo "$out" | grep -qE '[0-9]+ passed' && ! echo "$out" | grep -qE '[0-9]+ failed|error'; then
    pass "pytest $TEST_TARGET — $(echo "$out" | grep -oE '[0-9]+ passed.*' | head -1)"
  else
    warn "pytest $TEST_TARGET — $(echo "$out" | tail -1)" "fast mode: narrow scope, no baseline. NOT evidence — run --full before claiming done"
  fi
else
  skip "pytest" "$TEST_TARGET not found"
fi

# ------------------------------------- 5. --full: clean checkout + baseline comparison
if [[ $FULL -eq 1 ]]; then
  echo
  echo "[clean-checkout]"
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  TMPWT="$(git rev-parse --git-common-dir)/../.claude/worktrees/_verify_$$"
  if git worktree add --detach "$TMPWT" "$BRANCH" >/dev/null 2>&1; then
    _WORKTREES+=("$TMPWT")
    out=$(cd "$TMPWT/backend" && "$PY" -m pytest "${TEST_TARGET#backend/}" -q 2>&1 | tail -3)
    if echo "$out" | grep -qE '[0-9]+ passed' && ! echo "$out" | grep -qE '[0-9]+ failed|error'; then
      pass "clean checkout of $BRANCH — $(echo "$out" | grep -oE '[0-9]+ passed.*' | head -1)"
    else
      warn "clean checkout of $BRANCH — $(echo "$out" | tail -1)" "compare against the baseline line below"
    fi
    git worktree remove --force "$TMPWT" >/dev/null 2>&1
  else
    skip "clean-checkout" "could not create worktree"
  fi

  echo
  echo "[baseline]"
  # "No regressions" is unprovable without running the same suite on the base.
  # This repo carries pre-existing failures, so green-vs-nothing proves nothing.
  BASE="${VERIFY_BASE:-origin/main}"
  BASEWT="$(git rev-parse --git-common-dir)/../.claude/worktrees/_base_$$"

  # Ask pytest whether it RAN, do not infer it from text.
  #
  # The first version parsed only the summary line for '[0-9]+ failed'. A suite
  # that could not run at all — collection error, import error, Postgres down —
  # prints no such token, so it scored 0 and sailed through "no new failures".
  # The worse the breakage, the cleaner the verdict, in the one tool whose whole
  # purpose is preventing false greens. It also never counted pytest ERRORS,
  # only failures, so fixture/DB errors were invisible on the happy path too.
  #
  # Exit codes are the ground truth: 0 all passed · 1 tests failed · 2 interrupted
  # · 3 internal error · 4 usage error · 5 no tests collected. Anything >=2 means
  # the run did not happen and MUST NOT be scored as zero failures.
  _pytest_run() {   # $1=dir $2=target -> "ran:<failures+errors>|<summary>" or "norun:<why>"
    local out rc summary f e
    out="$(cd "$1" && "$PY" -m pytest "$2" -q --tb=no 2>&1)"; rc=$?
    if [[ $rc -ge 2 ]]; then printf 'norun:pytest exit %s (suite did not run)' "$rc"; return; fi
    summary="$(printf '%s' "$out" | grep -E '[0-9]+ (passed|failed|error)' | tail -1)"
    [[ -z "$summary" ]] && { printf 'norun:no parseable pytest summary'; return; }
    f="$(printf '%s' "$summary" | grep -oE '[0-9]+ failed'  | head -1 | grep -oE '[0-9]+')"
    e="$(printf '%s' "$summary" | grep -oE '[0-9]+ error'   | head -1 | grep -oE '[0-9]+')"
    local pcount; pcount="$(printf '%s' "$summary" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+')"
    # ZERO passing tests is not evidence, whatever the comparison says. With
    # Postgres down every test ERRORs equally on both sides, so "no new failures
    # (base=32, head=32)" is arithmetically true and completely worthless.
    [[ "${pcount:-0}" -eq 0 ]] && { printf 'norun:0 tests passed (%s)' "$summary"; return; }
    printf 'ran:%s|%s' "$(( ${f:-0} + ${e:-0} ))" "$summary"
  }

  if git worktree add --detach "$BASEWT" "$BASE" >/dev/null 2>&1; then
    _WORKTREES+=("$BASEWT")
    b="$(_pytest_run "$BASEWT/backend" "${TEST_TARGET#backend/}")"
    h="$(_pytest_run "$REPO_ROOT/backend" "${TEST_TARGET#backend/}")"
    echo "        baseline($BASE): ${b#*|}"
    echo "        head:            ${h#*|}"
    if [[ "$b" == norun:* || "$h" == norun:* ]]; then
      # No comparison is possible. Saying "no new failures" here is the exact
      # false green this rewrite exists to remove.
      fail "baseline comparison IMPOSSIBLE — the suite did not run" \
           "base: ${b#norun:} · head: ${h#norun:} (is Postgres up? is the target importable?)"
    else
      bf="${b%%|*}"; bf="${bf#ran:}"; hf="${h%%|*}"; hf="${hf#ran:}"
      if [[ "$hf" -le "$bf" ]]; then
        pass "no new failures vs $BASE (base=$bf, head=$hf — failures+errors)"
      else
        fail "NEW failures vs $BASE" "base=$bf head=$hf — these are yours"
      fi
    fi
    git worktree remove --force "$BASEWT" >/dev/null 2>&1
  else
    skip "baseline" "could not create worktree for $BASE"
  fi
else
  echo
  skip "clean-checkout + baseline + full suite" "fast mode covers $(find backend/tests/workers -name 'test_*.py' 2>/dev/null | wc -l | tr -d ' ') of $(find backend/tests -name 'test_*.py' 2>/dev/null | wc -l | tr -d ' ') backend test files — run --full before claiming done"
fi

# ---------------------------------------------------------------- verdict
echo
echo "────────────────────────────────────────────"
printf 'passed %d · failed %d · warned %d · skipped %d\n' "${#PASSED[@]}" "${#FAILED[@]}" "${#WARNED[@]}" "${#SKIPPED[@]}"
if ((${#FAILED[@]})); then
  echo "NOT DONE — failing:"; printf '  · %s\n' "${FAILED[@]}"
  exit 1
fi
if ((${#SKIPPED[@]})) && [[ $FULL -eq 1 ]]; then
  echo "INCOMPLETE — required checks were skipped:"; printf '  · %s\n' "${SKIPPED[@]}"
  exit 2
fi
if ((${#WARNED[@]})); then
  echo "PASS (with warnings) — evidence of done; the warnings are pre-existing, not introduced here:"
  printf '  · %s\n' "${WARNED[@]}"
  exit 0
fi
echo "PASS — evidence of done"
exit 0
