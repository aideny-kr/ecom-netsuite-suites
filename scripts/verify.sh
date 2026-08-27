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
ABORTED=0
cleanup() { [[ -n "$BASEWT" ]] && git worktree remove --force "$BASEWT" >/dev/null 2>&1; rm -rf "$TMP"; }
# INT/TERM get their OWN handler. Sharing one trap with EXIT meant a signal tore
# down $TMP and then let the script carry on to the verdict — which is how a
# killed run recorded PASS on 2026-08-27. An interrupted run knows nothing, so
# it must say nothing: no verdict, and a non-zero exit.
on_signal() {
  ABORTED=1
  echo "verify.sh: interrupted — NO verdict recorded (this run proves nothing)" >&2
  cleanup
  exit 130
}
trap cleanup EXIT
trap on_signal INT TERM

# Capture WHAT IS BEING VERIFIED once, at the start, and use it for both the banner
# and the evidence record. Re-querying HEAD when the run finishes attributes the
# verdict to whatever is checked out THEN — and a full run takes ~10 minutes, during
# which an agent can commit. Reproduced: banner said 6f50440, a commit landed
# mid-run, and the record was written against the new sha. That is a PASS for a
# commit the suite never saw, which is the exact lie this log exists to prevent.
#
# FULL 40-char sha, not --short: abbreviation width auto-scales with object count,
# so the same commit was recorded as both 44e68db and 44e68dbf, and the consumers'
# lookup silently missed the older entry. Readers match by prefix.
VERIFIED_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
VERIFIED_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "verify.sh — $VERIFIED_BRANCH @ ${VERIFIED_SHA:0:8}"

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
    out="$(cd "$1/backend" && "$PY" -m pytest tests -q --tb=no -rfE 2>&1)"; rc=$?
    # exit >=2 means pytest could not run at all (collection/usage/internal).
    # Scoring that as "0 failures" is how a totally broken suite passed before.
    [[ $rc -ge 2 ]] && { echo "DID-NOT-RUN (pytest exit $rc)"; return 2; }
    printf '%s\n' "$out" | grep -E '^(FAILED|ERROR)' | awk '{print $2}' | sort -u > "$2"
    # Zero passing tests is not evidence however the comparison reads.
    local psd; psd="$(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+')"
    [[ "${psd:-0}" -eq 0 ]] && { echo "NOTHING PASSED ($(printf '%s' "$out" | tail -1))"; return 2; }
    printf '%s' "$out" | grep -E '[0-9]+ (passed|failed|error)' | tail -1
  }
  hs="$(_failing "$REPO_ROOT" "$TMP/head.ids")"; hrc=$?
  echo "        head:     $hs"
  # A run with many ERRORs (DB contention, missing service) still supports the
  # by-id comparison, but it is weak evidence — say so rather than let a clean
  # "no NEW failing tests" imply a healthy suite.
  _herr="$(printf '%s' "$hs" | grep -oE '[0-9]+ error' | head -1 | grep -oE '[0-9]+' || true)"
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
        _berr="$(printf '%s' "$bs" | grep -oE '[0-9]+ error' | head -1 | grep -oE '[0-9]+' || true)"
        # An ERROR means the test never ran, so there is NO EVIDENCE about it —
        # whatever the comparison says. Both sides having 1208 errors makes
        # "no new failing tests" true and "evidence of done" false at the same
        # time, and this script's contract is the second one. A healthy run of
        # this suite is 5587 passed / 0 errors, so errors always mean the
        # environment is broken, never a standing condition to tolerate.
        if [[ "${_herr:-0}" -gt "${_berr:-0}" ]]; then
          fail "MORE test ERRORs than $BASE (base=${_berr:-0}, head=${_herr:-0}) — these are yours"
        elif [[ "${_herr:-0}" -gt 0 ]]; then
          fail "${_herr} tests ERRORed — they did not run, so this is not evidence" \
               "same count on $BASE, so not introduced here — but fix the environment (is Postgres up?) before claiming done"
        fi
        # comm on a MISSING file writes to stderr and prints NOTHING — and an
        # empty `new` is the success condition three lines down. So a vanished
        # temp file (an interrupted run, a cleanup that fired early, a full
        # disk) reads as "no new failing tests" and this script reports PASS
        # having compared nothing. Observed 2026-08-27: a killed run logged
        # `comm: .../head.ids: No such file` immediately followed by
        # `PASS  no NEW failing tests`, and recorded PASS for that sha — the
        # exact lie the evidence log exists to prevent, written BY the thing
        # that prevents it. Check the inputs before trusting their comparison.
        if [[ ! -r "$TMP/head.ids" || ! -r "$TMP/base.ids" ]]; then
          fail "comparison inputs are missing — nothing was compared" \
               "head.ids readable=$([[ -r "$TMP/head.ids" ]] && echo yes || echo NO), base.ids readable=$([[ -r "$TMP/base.ids" ]] && echo yes || echo NO)"
        else
        new="$(comm -23 "$TMP/head.ids" "$TMP/base.ids")"
        fixedids="$(comm -13 "$TMP/head.ids" "$TMP/base.ids")"
        fixed="$(printf '%s' "$fixedids" | grep -c . || true)"
        if [[ -z "$new" ]]; then
          pass "no NEW failing tests vs $BASE (${fixed} pre-existing now fixed)"
          # NAME them. A test that fails on the base and passes here is one of three
          # things — you fixed it, it is FLAKY, or it depends on ambient state — and
          # only the first is good news. Reporting a bare count reads as the first.
          # Observed 2026-08-07: a branch touching only scripts/ reported "1
          # pre-existing now fixed", which it cannot possibly have fixed; without the
          # id there was nothing to chase, and a flaky baseline quietly makes every
          # future comparison noisy in both directions.
          if [[ -n "$fixedids" ]]; then
            note "these FAILED on $BASE but pass here — verify each is really a fix, not flake:"
            printf '        %s\n' $fixedids | head -8
          fi
        else
          fail "NEW failing tests vs $BASE — these are yours" "$(echo "$new" | head -8)"
        fi
        fi
      fi
    else fail "could not create baseline worktree for $BASE"; fi
  fi
fi

# --- the record ------------------------------------------------------------
# THIS script writes the evidence log. It used to be scraped out of stdout by a
# PostToolUse hook, which failed in both directions and was trusted anyway:
#
#   MISSED real runs — a backgrounded run returns "Command running in background"
#     to the tool layer, so the banner never reached the hook. Nearly every run on
#     2026-08-06 was backgrounded and none of them recorded.
#   INVENTED fake runs — `tail`ing a saved output file echoes the banner, and the
#     hook dutifully logged a run that never happened. Entries from an unrelated
#     branch sit in the log with verdict UNKNOWN for the same reason.
#
# A verdict is a fact the producer knows. Scraping a consumer's stdout to recover
# it is fragile by construction: it depends on how the command was invoked, which
# has nothing to do with whether the suite ran. So verify.sh appends its own line
# and stop_guard.py reads it — one producer, one consumer, no inference.
record() {  # $1 = verdict
  local dir tree
  # Belt to on_signal's braces: if anything set ABORTED, this run compared
  # nothing and must leave no evidence behind. Silence is the correct output
  # for a run that was cut short — the Stop hook then correctly reports "no
  # PASS recorded", which is true.
  if [[ "${ABORTED:-0}" -eq 1 ]]; then
    echo "verify.sh: aborted run — leaving no evidence line" >&2
    return 0
  fi
  dir="$(git rev-parse --git-common-dir 2>/dev/null)/verify-runs"
  # Whether the working tree was clean. A PASS earned at a sha does NOT cover
  # uncommitted edits sitting on top of it, and a fresh branch starts at its base
  # commit — so without this a `git checkout -b` inherits the base's PASS for work
  # that has never been tested.
  tree="clean"; [[ -n "$(git status --porcelain 2>/dev/null | grep -v '^??')" ]] && tree="dirty"
  if ! mkdir -p "$dir" 2>/dev/null; then
    echo "verify.sh: WARNING — cannot create $dir; this run leaves NO evidence" >&2
    return 0
  fi
  if ! printf '%s %s@%s %s quick=%s tree=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$VERIFIED_BRANCH" "$VERIFIED_SHA" \
    "$1" "$([[ $QUICK -eq 1 ]] && echo yes || echo no)" "$tree" >> "$dir/log" 2>/dev/null
  then
    # Silence here is the worst outcome: verify.sh prints PASS and exits 0, then the
    # Stop hook blocks with "no PASS recorded for HEAD" — the two most authoritative
    # voices contradicting each other with no explanation.
    echo "verify.sh: WARNING — could not append to $dir/log; this run leaves NO evidence" >&2
  fi
}

# --- verdict ---------------------------------------------------------------
echo; echo "────────────────────────────────────────────"
printf 'passed %d · failed %d · notes %d\n' "${#PASSED[@]}" "${#FAILED[@]}" "${#NOTES[@]}"
if ((${#FAILED[@]})); then printf '  · %s\n' "${FAILED[@]}"; record NOT-DONE; echo "NOT DONE"; exit 1; fi
[[ $QUICK -eq 1 ]] && { record QUICK-ONLY; echo "QUICK ONLY — run without --quick before claiming done"; exit 2; }
record PASS
echo "PASS — evidence of done"; exit 0
