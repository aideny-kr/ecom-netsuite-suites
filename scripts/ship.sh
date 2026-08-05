#!/usr/bin/env bash
# ship.sh — the development graph's terminal edges, as code.
#
# The graph:
#
#   FRAME ──→ SCOPE ──→ BUILD ⇄ VERIFY ──→ GATE ──→ LAND
#   (human)   (tier)            (evidence)  (T2 only)
#
# FRAME and SCOPE are human/judgement nodes. This script owns the last three edges,
# which are the ones that failed repeatedly on 2026-08-02:
#
#   BUILD → GATE   must not be traversable without passing VERIFY
#   SCOPE → tier   must be COMPUTED from the diff, not recalled from a prose list
#   GATE  → LAND   must not be traversable on a gate result that reviewed the wrong branch
#
# Design note: this is structure, not restriction. It does not block anything — it makes
# the correct path a single command, so doing it right is easier than doing it wrong.
# A rule that nags is just more prose to ignore; a command that does the work gets used.
#
#   ./scripts/ship.sh            # compute tier, run verify --full, print the gate command
#   ./scripts/ship.sh --fast     # verify fast mode (NOT sufficient to land)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BASE="${SHIP_BASE:-origin/main}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
MODE="--full"; [[ "${1:-}" == "--fast" ]] && MODE=""


echo "ship.sh — $BRANCH vs $BASE"
echo

# ─────────────────────────────────────────────── edge 1: what actually changed
CHANGED="$(git diff --name-only "$BASE...HEAD" 2>/dev/null)"
if [[ -z "$CHANGED" ]]; then
  echo "No commits vs $BASE. Nothing to ship."
  exit 1
fi
DIRTY="$(git status --porcelain | grep -vE '^\?\?' || true)"
echo "[scope] $(echo "$CHANGED" | wc -l | tr -d ' ') files changed vs $BASE"
if [[ -n "$DIRTY" ]]; then
  echo "  UNCOMMITTED changes present — the gate reviews commits, so these would NOT be reviewed:"
  echo "$DIRTY" | sed 's/^/    /' | head -8
  echo "  Commit or stash before shipping."
  exit 1
fi

# ───────────────────────────────────── edge 2: tier is COMPUTED, not remembered
# The T2 trigger list in CLAUDE.md is prose I have to recall correctly under load.
# Recall is the failure mode; computing it from the diff is not.
# Tier is decided by the human, against CLAUDE.md's list — not by a copy of it.
# The previous version re-implemented the T2 trigger checklist as path regexes,
# which .claude/rules/uat-review.md explicitly forbids ("do NOT duplicate this
# list"), and the copy immediately drifted: backend/app/api, services/chat,
# models and scripts/ all computed T1 — "no blocking gate required" — on this
# repo's highest-risk surfaces. A second source of truth is worse than none,
# because it is trusted.
echo
echo "[tier] decide against the canonical list in CLAUDE.md:"
sed -n '/^\*\*T2 (high-risk)\*\*/,/^\*\*T1\*\*/p' CLAUDE.md | fold -s -w 88 | sed 's/^/  /'
echo
echo "  files changed:"
echo "$CHANGED" | sed 's/^/    /'

# ─────────────────────────────────── edge 3: BUILD → GATE requires VERIFY to pass
echo
echo "[verify] running scripts/verify.sh $MODE"
echo "────────────────────────────────────────────"
# shellcheck disable=SC2086
./scripts/verify.sh $MODE
VERIFY_RC=$?
echo "────────────────────────────────────────────"
if [[ $VERIFY_RC -ne 0 ]]; then
  echo
  echo "STOP — verify failed (rc=$VERIFY_RC). This edge is not traversable."
  echo "Fix the failures and re-run. Do not proceed to the gate on unverified work."
  exit 1
fi
if [[ -z "$MODE" ]]; then
  echo
  echo "STOP — ran in --fast mode. Fast mode skips the clean-checkout and baseline"
  echo "comparison, so it cannot distinguish your regressions from pre-existing ones."
  echo "Re-run without --fast before landing."
  exit 2
fi

# ───────────────────────────────────────── edge 4: GATE, with target PINNED
echo
if [[ "${TIER:-T2}" == "T2" ]]; then
  cat <<EOF
[gate] $TIER requires the blocking multi-angle review BEFORE merge.

Run it with the target PINNED — the gate defaults to the session cwd's branch, and
in this repo the session usually sits in a different worktree, which already produced
a full 59-agent run against an unrelated branch:

  Workflow({name: "code-review-multiangle",
            args: {target: "$BRANCH", base: "$BASE"}})

Then, before believing ANY finding, check the result:
  · target == "$BRANCH"      (target: null ⇒ wrong branch ⇒ the run is void)
  · base   == "$BASE"
  · status == "OK"           (INCOMPLETE/PREP_FAILED ⇒ not a pass, re-run)
  · codex_used == true       (false ⇒ no independent model actually reviewed)
  · failed_angles == []

One clean round is weak evidence — observed 0 → 2 → 3 → 1 → 0 across rounds on a
single PR. Budget for at least two.
EOF
else
  echo "[gate] $TIER — no blocking gate required. CI is the check."
fi

echo
echo "[land] after the gate clears:"
echo "  · update STATE.md (NOW / NEXT / DECIDED / DON'T)"
echo "  · record anything non-obvious — prefer a docstring next to the code over a rule"
echo
echo "verify PASSED · tier $TIER · ready for the gate"
exit 0
