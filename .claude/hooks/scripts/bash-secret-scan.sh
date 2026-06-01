#!/usr/bin/env bash
# PostToolUse hook for Bash.
#
# detect-secrets.sh catches "Claude wrote a secret via Edit/Write/MultiEdit".
# Bash bypasses that entirely (`echo SECRET > file`, code generators, package
# tools, here-docs). This is the post-hoc scan: look at file paths the bash
# command touched and grep them for the same patterns.
#
# We cannot block the action (PostToolUse fires after the side-effect), but
# exit 2 + stderr feeds context back to Claude so the next turn sees the
# warning and remediates.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // ""')"
[[ "$tool_name" == "Bash" ]] || exit 0

cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')"
[[ -z "$cmd" ]] && exit 0

# Detect redirects so a "read-only" command is no longer skipped when it
# actually writes a file (e.g. ``cat secret.txt > newfile``). Only skip when
# the entire command is read-only AND has no redirection.
has_redirect=0
case "$cmd" in
    *">"*|*">>"*|*"tee "*) has_redirect=1 ;;
esac

if (( has_redirect == 0 )); then
    case "$cmd" in
        "ls"*|"cat"*|"grep"*|"rg"*|"find"*|"head"*|"tail"*|"wc"*|"git status"*|"git diff"*|"git log"*|"git show"*|"git ls-files"*|"git branch"*|"git fetch"*|"git remote"*) exit 0 ;;
    esac
fi

# Cheap heuristic: scan for files referenced by the command line that exist on
# disk and are smaller than 1MB. Avoids walking the whole repo.
candidates=()
for tok in $cmd; do
    # Strip leading > redirects and quotes.
    tok="${tok#>>}"
    tok="${tok#>}"
    tok="${tok#\"}"; tok="${tok%\"}"
    tok="${tok#\'}"; tok="${tok%\'}"
    if [[ -f "$tok" ]]; then
        size=$(wc -c < "$tok" 2>/dev/null || echo 0)
        if (( size > 0 && size < 1048576 )); then
            candidates+=("$tok")
        fi
    fi
done

(( ${#candidates[@]} == 0 )) && exit 0

# Patterns live in lib/secret-patterns.txt so detect-secrets.sh and
# bash-secret-scan.sh stay in lockstep.
PATTERNS_FILE="$(dirname "$0")/lib/secret-patterns.txt"
[[ -f "$PATTERNS_FILE" ]] || exit 0

matches=()
while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    label="${line%%::*}"
    pattern="${line#*::}"
    for f in "${candidates[@]}"; do
        if grep -E -q -e "$pattern" "$f" 2>/dev/null; then
            matches+=("$label in $f")
        fi
    done
done <"$PATTERNS_FILE"

if (( ${#matches[@]} == 0 )); then
    exit 0
fi

{
    echo "WARNING from bash-secret-scan (post-hoc; file already written):"
    for m in "${matches[@]}"; do
        echo "  - $m"
    done
    echo
    echo "Remove the secret immediately. If it was committed, rotate the credential."
} >&2
exit 2
