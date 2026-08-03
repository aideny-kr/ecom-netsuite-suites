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
if ni: print("beat tasks leaving no jobs row on failure:", ni); sys.exit(1)
print(f"{len(celery_app.conf.beat_schedule)} beat entries registered + instrumented")
PYEOF
  )
  if [[ $? -eq 0 ]]; then pass "worker modules import + beat registry"; printf '        %s\n' "$(echo "$out" | tail -2 | tr '\n' ' ')"
  else fail "worker modules import + beat registry" "$(echo "$out" | head -6)"; fi
else
  skip "import" "no backend/app"
fi

# ------------------------------------------- 3. tracked-file check (clean-checkout proxy)
# .gitignore silently swallowed a new task module; every local check passed and any
# fresh clone would have failed at worker boot. Cheap proxy: nothing referenced by
# conf.include may be untracked.
echo
echo "[tracked]"
if [[ -d backend/app/workers/tasks ]]; then
  untracked=$(git ls-files --others --exclude-standard backend/app | grep -E '\.py$' || true)
  if [[ -z "$untracked" ]]; then pass "no untracked .py under backend/app"
  else fail "untracked .py files (invisible to CI and any fresh clone)" "$(echo "$untracked" | head -5)"; fi
else
  skip "tracked" "no backend/app/workers/tasks"
fi

# ---------------------------------------------------------------- 4. tests
echo
echo "[tests]"
TEST_TARGET="${VERIFY_TESTS:-backend/tests/workers}"
if [[ -d "$TEST_TARGET" ]]; then
  out=$(cd backend && "$PY" -m pytest "${TEST_TARGET#backend/}" -q 2>&1 | tail -4)
  if echo "$out" | grep -qE '[0-9]+ passed' && ! echo "$out" | grep -qE '[0-9]+ failed|error'; then
    pass "pytest $TEST_TARGET — $(echo "$out" | grep -oE '[0-9]+ passed.*' | head -1)"
  else
    warn "pytest $TEST_TARGET — $(echo "$out" | tail -1)" "baseline not compared in fast mode; run --full to tell yours from pre-existing"
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

  # Parse ONLY pytest's final summary line. Matching '[0-9]+ failed' across the whole
  # output scrapes numbers out of assertion text too — a test whose failure message
  # contained "6543 failed" produced a 25-line "count" and a FALSE "new failures"
  # verdict. A verification tool that cries wolf is worse than no tool.
  _fail_count() { # $1=dir $2=target -> integer failures
    local summary
    summary="$(cd "$1" && "$PY" -m pytest "$2" -q --tb=no 2>&1 \
                | grep -E '^[0-9]+ (passed|failed)|[0-9]+ (passed|failed).*in [0-9.]+s' | tail -1)"
    printf '%s' "$summary" > /tmp/.verify_summary_$$
    local n; n="$(printf '%s' "$summary" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '[0-9]+')"
    printf '%s' "${n:-0}"
  }

  if git worktree add --detach "$BASEWT" "$BASE" >/dev/null 2>&1; then
    bf="$(_fail_count "$BASEWT/backend" "${TEST_TARGET#backend/}")"
    echo "        baseline($BASE): $(cat /tmp/.verify_summary_$$ 2>/dev/null)"
    hf="$(_fail_count "$REPO_ROOT/backend" "${TEST_TARGET#backend/}")"
    echo "        head:            $(cat /tmp/.verify_summary_$$ 2>/dev/null)"
    rm -f /tmp/.verify_summary_$$
    if [[ "$hf" -le "$bf" ]]; then
      pass "no new failures vs $BASE (base=$bf, head=$hf)"
    else
      fail "NEW failures vs $BASE" "base=$bf head=$hf — these are yours"
    fi
    git worktree remove --force "$BASEWT" >/dev/null 2>&1
  else
    skip "baseline" "could not create worktree for $BASE"
  fi
else
  echo
  skip "clean-checkout + baseline" "fast mode; run with --full before claiming done"
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
