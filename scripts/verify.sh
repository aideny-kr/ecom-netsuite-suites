#!/usr/bin/env bash
# verify.sh — the development loop's evidence node. Exit 0 means done.
#
# Every check here has been demonstrated to catch a real bug from this repo.
# Checks that merely LOOKED like they verified something were removed on
# 2026-08-04, after two review rounds found three of them could not detect the
# very bug their own comments cited. If you add a check, prove it goes red
# against the broken code first. That is the whole standard.
#
#   ./scripts/verify.sh          # lint + modules + full suite vs baseline
#   ./scripts/verify.sh --quick  # skips the suite. NOT evidence; inner loop only.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
QUICK=0; [[ "${1:-}" == "--quick" ]] && QUICK=1

# --- interpreter -----------------------------------------------------------
# Worktrees carry no backend/.venv (it lives in the main checkout); falling
# through to system python silently runs every check without celery or pytest.
MAIN="$(cd "$(git rev-parse --git-common-dir)/.." 2>/dev/null && pwd || echo "$REPO_ROOT")"
for c in "${VERIFY_PYTHON:-}" "$REPO_ROOT/backend/.venv/bin/python" "$MAIN/backend/.venv/bin/python"; do
  [[ -n "$c" && -x "$c" ]] && { PY="$c"; break; }
done
PY="${PY:-$(command -v python3)}"
RUFF="$(dirname "$PY")/ruff"; [[ -x "$RUFF" ]] || RUFF="$(command -v ruff || true)"
"$PY" -c "import celery" >/dev/null 2>&1 || { echo "verify.sh: '$PY' lacks project deps; set VERIFY_PYTHON"; exit 2; }

# --- database --------------------------------------------------------------
# All THREE urls, and fail closed on anything remote. InstrumentedTask.before_start
# WRITES a Job row and an AuditEvent through DATABASE_URL_SYNC; a normal .env points
# that at the live pooler, so overriding only the async urls had every
# instrumented-task test inserting rows into the production database.
export DATABASE_URL="${VERIFY_DB:-postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite}"
export DATABASE_URL_DIRECT="$DATABASE_URL"
export DATABASE_URL_SYNC="${VERIFY_DB_SYNC:-postgresql://postgres:postgres@localhost:5432/ecom_netsuite}"
for u in "$DATABASE_URL" "$DATABASE_URL_DIRECT" "$DATABASE_URL_SYNC"; do
  case "$u" in *localhost*|*127.0.0.1*) ;; *)
    echo "verify.sh: refusing — DB url is not local: $(printf '%s' "$u" | sed -E 's#//[^@]*@#//***@#')"; exit 2 ;;
  esac
done

FAILED=(); PASSED=(); NOTES=()
pass() { PASSED+=("$1"); printf '  PASS  %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }
note() { NOTES+=("$1");  printf '  NOTE  %s\n' "$1"; }   # true, but not caused by this diff

TMP="${TMPDIR:-/tmp}/verify.$$"; mkdir -p "$TMP"
BASEWT=""
cleanup() { [[ -n "$BASEWT" ]] && git worktree remove --force "$BASEWT" >/dev/null 2>&1; rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

echo "verify.sh — $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"

# --- 1. lint ---------------------------------------------------------------
# Lint only what THIS branch changed. Repo-wide lint made verify.sh red over two
# pre-existing errors in files the branch never touched (W292/I001 in
# oracle_skill_reseed.py and report_auto_refresh.py), which is the permanently-red
# trap — the same reason the test check compares against a baseline rather than
# an absolute count. CI still lints the whole tree; that is CI's job, not this
# gate's. Note ruff prints "All checks passed!" for paths that do not exist, so an
# empty file list must skip rather than silently succeed.
echo; echo "[lint]"
CHANGED_PY="$(git diff --name-only "${VERIFY_BASE:-origin/main}"...HEAD -- '*.py' | while read -r f; do [[ -f "$f" ]] && echo "$f"; done)"
if [[ ! -x "$RUFF" ]]; then fail "ruff not found at $RUFF"
elif [[ -z "$CHANGED_PY" ]]; then note "no .py files changed on this branch — nothing to lint"
else
  # CI runs BOTH check and format --check; a green `check` alone is not enough.
  echo "$CHANGED_PY" | xargs "$RUFF" check >/dev/null 2>&1 \
    && pass "ruff check ($(echo "$CHANGED_PY" | wc -l | tr -d ' ') changed files)" || fail "ruff check"
  echo "$CHANGED_PY" | xargs "$RUFF" format --check >/dev/null 2>&1 \
    && pass "ruff format --check" || fail "ruff format --check"
fi

# --- 2. modules import, and are tracked ------------------------------------
# PROVEN twice: catches a module-scope NameError (a syntactic scan cannot), and a
# gitignored worker module (`git ls-files --others --exclude-standard` cannot —
# --exclude-standard excludes exactly the ignored files it was hunting).
echo; echo "[modules]"
cat > "$TMP/modules.py" <<'PYEOF'
import importlib, importlib.util, os, subprocess, sys
try:
    from app.workers.celery_app import celery_app
except Exception as e:
    print(f"celery_app failed to import: {type(e).__name__}: {e}"); sys.exit(1)

bad = []
for m in celery_app.conf.include:
    try: importlib.import_module(m)
    except Exception as e: bad.append(f"{m}: {type(e).__name__}: {e}")
if bad:
    print("conf.include modules that fail to import (worker will NOT boot):")
    [print("   " + b) for b in bad]; sys.exit(1)

root = subprocess.run(["git","rev-parse","--show-toplevel"],capture_output=True,text=True).stdout.strip()
# Guard the .pth trap explicitly: if a module resolved OUTSIDE this checkout the
# answer is meaningless, so say that instead of reporting it as untracked.
_stray = [m for m in celery_app.conf.include
          if (sp := importlib.util.find_spec(m)) and sp.origin
          and not os.path.realpath(sp.origin).startswith(os.path.realpath(root) + os.sep)]
if _stray:
    print(f"modules resolved OUTSIDE this checkout ({root}) — venv .pth is pointing elsewhere:")
    [print("   " + m) for m in _stray[:4]]; sys.exit(1)
untracked = []
for m in celery_app.conf.include:
    spec = importlib.util.find_spec(m)
    if not spec or not spec.origin: continue
    rel = os.path.relpath(spec.origin, root)
    if subprocess.run(["git","ls-files","--error-unmatch",rel],capture_output=True,cwd=root).returncode:
        untracked.append(f"{m} -> {rel}")
if untracked:
    print("modules Celery loads that git does NOT track (fails on any clean clone):")
    [print("   " + u) for u in untracked]; sys.exit(1)

print(f"{len(celery_app.conf.include)} modules import and are tracked")
PYEOF
out=$(cd backend && PYTHONPATH="$PWD" "$PY" "$TMP/modules.py" 2>&1)
if [[ $? -eq 0 ]]; then pass "$(echo "$out" | tail -1)"; else fail "worker modules" "$(echo "$out" | head -5)"; fi

# --- 3. the suite, compared to the baseline BY TEST ID ---------------------
# Counts are not enough. An equal-count swap (fix two, break two) reads as "no new
# failures", and marking a failing test @skip LOWERS the count and scores as an
# improvement — an active incentive to silence tests inside a retry loop.
# Compare the SET of failing node ids.
echo; echo "[tests]"
if [[ $QUICK -eq 1 ]]; then
  note "suite skipped (--quick) — this run is NOT evidence"
else
  _failing() {  # $1=worktree root, $2=ids outfile. echoes summary. rc 2 = did not run.
    local out rc
    out="$(cd "$1/backend" && "$PY" -m pytest tests -q --tb=no -rf 2>&1)"; rc=$?
    # exit >=2 means pytest could not run at all (collection/usage/internal).
    # Scoring that as "0 failures" is how a totally broken suite passed before.
    [[ $rc -ge 2 ]] && { echo "DID-NOT-RUN (pytest exit $rc)"; return 2; }
    printf '%s\n' "$out" | grep -E '^(FAILED|ERROR)' | awk '{print $2}' | sort -u > "$2"
    printf '%s' "$out" | grep -E '[0-9]+ (passed|failed|error)' | tail -1
  }
  hs="$(_failing "$REPO_ROOT" "$TMP/head.ids")"; hrc=$?
  echo "        head:     $hs"
  # A run with many ERRORs (DB contention, missing service) still supports the
  # by-id comparison, but it is weak evidence — say so rather than let a clean
  # "no NEW failing tests" imply a healthy suite.
  _errs="$(printf '%s' "$hs" | grep -oE '[0-9]+ error' | head -1 | grep -oE '[0-9]+' || true)"
  [[ "${_errs:-0}" -gt 0 ]] && note "$_errs test ERRORs on head — comparison is valid but the suite is degraded (is Postgres healthy?)"
  if [[ $hrc -eq 2 ]]; then
    fail "the suite did not run" "$hs"
  else
    BASE="${VERIFY_BASE:-origin/main}"; BASEWT="$TMP/base"
    if git worktree add --detach "$BASEWT" "$BASE" >/dev/null 2>&1; then
      bs="$(_failing "$BASEWT" "$TMP/base.ids")"; brc=$?
      echo "        baseline: $bs"
      if [[ $brc -eq 2 ]]; then
        fail "baseline suite did not run — no comparison possible" "$bs"
      else
        new="$(comm -23 "$TMP/head.ids" "$TMP/base.ids")"
        fixed="$(comm -13 "$TMP/head.ids" "$TMP/base.ids" | grep -c . || true)"
        if [[ -z "$new" ]]; then
          pass "no NEW failing tests vs $BASE (${fixed} pre-existing now fixed)"
        else
          fail "NEW failing tests vs $BASE — these are yours" "$(echo "$new" | head -8)"
        fi
      fi
    else fail "could not create baseline worktree for $BASE"; fi
  fi
fi

# --- verdict ---------------------------------------------------------------
echo; echo "────────────────────────────────────────────"
printf 'passed %d · failed %d · notes %d\n' "${#PASSED[@]}" "${#FAILED[@]}" "${#NOTES[@]}"
if ((${#FAILED[@]})); then printf '  · %s\n' "${FAILED[@]}"; echo "NOT DONE"; exit 1; fi
[[ $QUICK -eq 1 ]] && { echo "QUICK ONLY — run without --quick before claiming done"; exit 2; }
echo "PASS — evidence of done"; exit 0
