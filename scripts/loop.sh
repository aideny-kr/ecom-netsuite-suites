#!/usr/bin/env bash
# loop.sh — the development loop's STOPPING RULE, in state rather than in prose.
#
# The loop is: build -> verify -> specific feedback -> bounded retry -> exit.
# verify.sh is the evidence node. This is the exit node, and it was the missing one.
#
# CLAUDE.md has said "max 15 iterations per task, stop after 3 self-heal attempts"
# for months. Nothing enforced it. A cap the model is asked to respect is a request;
# a counter that persists and decrements is a guarantee — and termination is the one
# thing no model derives for itself at any capability, because it is the halting
# problem wearing a hat. A human sets it.
#
#   ./scripts/loop.sh start "ship the reject endpoint"   # opens a task, resets counters
#   ./scripts/loop.sh attempt                            # call before each build/fix cycle
#   ./scripts/loop.sh check                              # runs verify.sh, decides exit
#   ./scripts/loop.sh status                             # where am I
#   ./scripts/loop.sh end <reason>                       # done|budget|stall|error|blocked
#
# Exit REASON, never a boolean: "stopped" merges "finished" with "stuck", and a
# human cannot route what they cannot distinguish. Measured: ~71% of real stops
# are stalls, which a boolean would misreport as a budget problem.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
STATE="${LOOP_STATE:-$REPO_ROOT/.loop-state.json}"

MAX_ATTEMPTS="${LOOP_MAX_ATTEMPTS:-15}"   # total build/fix cycles on one task
MAX_SELF_HEAL="${LOOP_MAX_SELF_HEAL:-3}"  # consecutive failures with no new evidence

_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
_read() { [[ -f "$STATE" ]] && cat "$STATE" || echo '{}'; }
_get()  { _read | python3 -c "import json,sys;print(json.load(sys.stdin).get('$1',''))" 2>/dev/null; }
_put()  { python3 - "$@" <<'PY'
import json,sys,os
path=os.environ["STATE"]
try: d=json.load(open(path))
except Exception: d={}
for kv in sys.argv[1:]:
    k,_,v=kv.partition("=")
    if v in ("true","false"): v = v=="true"
    elif v.isdigit(): v=int(v)
    d[k]=v
json.dump(d, open(path,"w"), indent=2)
PY
}
export STATE

cmd="${1:-status}"

case "$cmd" in

start)
  task="${2:-}"
  [[ -z "$task" ]] && { echo "loop.sh start \"<what you are doing>\""; exit 2; }
  # FRAME is a human node. The loop refuses to open without a stated goal, because
  # an unstated goal is what lets a session drift into adjacent work and call it
  # progress — which is exactly how 2026-08-02 was spent.
  STATE="$STATE" _put "task=$task" "attempts=0" "self_heal=0" "started=$(_now)" \
                      "last_evidence=" "reason=" "ended="
  echo "loop opened: $task"
  echo "  budget: $MAX_ATTEMPTS attempts · $MAX_SELF_HEAL consecutive self-heals"
  ;;

attempt)
  [[ -z "$(_get task)" ]] && { echo "no open task — run: loop.sh start \"...\""; exit 2; }
  n=$(( $(_get attempts) + 1 ))
  STATE="$STATE" _put "attempts=$n"
  if (( n > MAX_ATTEMPTS )); then
    STATE="$STATE" _put "reason=budget" "ended=$(_now)"
    echo "STOP reason=budget — $n attempts exceeds $MAX_ATTEMPTS."
    echo "  This is not a failure to try harder. Re-frame the task or escalate."
    exit 1
  fi
  echo "attempt $n/$MAX_ATTEMPTS · self-heal $(_get self_heal)/$MAX_SELF_HEAL"
  ;;

check)
  [[ -z "$(_get task)" ]] && { echo "no open task"; exit 2; }
  before="$(_get last_evidence)"
  out="$(./scripts/verify.sh "${2:---full}" 2>&1)"; rc=$?
  echo "$out" | tail -12

  # Fingerprint the failure set. Retrying against an IDENTICAL failure is a stall,
  # not progress — this is what distinguishes "converging slowly" from "stuck",
  # and it is the distinction a bare attempt counter cannot make.
  # Strip volatile text before hashing. The WARN lines embed pytest's wall-clock
  # duration ("... in 13.84s"), so hashing them raw produced a different digest
  # every run and the stall rule — the whole point of this file — could never
  # fire. Also include SKIP lines: verify.sh's exit-2 path emits only those.
  now_ev="$(echo "$out" \
    | grep -E '^  (FAIL|WARN|SKIP)' \
    | sed -E 's/in [0-9.]+s.*//; s/\([0-9:]+\)//g; s/[0-9]+\.[0-9]+s//g' \
    | sort | shasum | cut -c1-12)"
  STATE="$STATE" _put "last_evidence=$now_ev"

  if [[ $rc -eq 0 ]]; then
    STATE="$STATE" _put "reason=done" "ended=$(_now)"
    echo
    echo "STOP reason=done — evidence passed."
    exit 0
  fi

  if [[ "$now_ev" == "$before" && -n "$before" ]]; then
    sh=$(( $(_get self_heal) + 1 ))
    STATE="$STATE" _put "self_heal=$sh"
    if (( sh >= MAX_SELF_HEAL )); then
      STATE="$STATE" _put "reason=stall" "ended=$(_now)"
      echo
      echo "STOP reason=stall — $sh consecutive attempts produced the SAME failures."
      echo "  Re-running will not help. Change approach or escalate to a human."
      exit 1
    fi
    echo
    echo "same failures as last attempt (self-heal $sh/$MAX_SELF_HEAL)"
  else
    STATE="$STATE" _put "self_heal=0"
    echo
    echo "failures changed — progress. continuing."
  fi
  exit 1
  ;;

end)
  reason="${2:-}"
  case "$reason" in
    done|budget|stall|error|blocked) ;;
    *) echo "reason must be one of: done budget stall error blocked"; exit 2 ;;
  esac
  STATE="$STATE" _put "reason=$reason" "ended=$(_now)"
  echo "loop closed: $(_get task) · reason=$reason · attempts=$(_get attempts)"
  echo "Record it in STATE.md — a closed loop nobody wrote down is a loop that repeats."
  ;;

status)
  t="$(_get task)"
  if [[ -z "$t" ]]; then echo "no open task"; exit 0; fi
  printf 'task:      %s\nattempts:  %s/%s\nself-heal: %s/%s\nreason:    %s\n' \
    "$t" "$(_get attempts)" "$MAX_ATTEMPTS" "$(_get self_heal)" "$MAX_SELF_HEAL" \
    "$(_get reason || echo '(open)')"
  ;;

*) echo "usage: loop.sh {start <task>|attempt|check|end <reason>|status}"; exit 2 ;;
esac
