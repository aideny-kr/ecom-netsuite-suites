#!/usr/bin/env bash
# PreToolUse hook for Edit | Write | MultiEdit.
#
# Blocks any write that introduces well-known secret tokens — Stripe live keys,
# AWS access keys, Anthropic / OpenAI keys, generic-looking PEM blocks, etc.
# This is the deterministic safety layer that the CLAUDE.md "never commit
# secrets" rule could only state advisorily.
#
# Anthropic hook contract:
#   stdin  = JSON event from Claude Code
#   exit 0 = allow (stdout JSON optional)
#   exit 2 = block (stderr goes back to Claude)
#
# Defensive design choices:
#   - if jq is missing, allow the call. We refuse to wedge the agent over a
#     tooling gap; a separate health-check belongs in CI.
#   - patterns are anchored where possible to avoid false positives on
#     placeholder strings like "ANTHROPIC_API_KEY=...".
#   - .env writes are *not* policed here; they're already blocked by
#     permissions.deny in .claude/settings.json. This hook covers the
#     "Claude pasted a literal secret into a normal source file" case.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // ""')"

# Build the candidate string by concatenating every text field the tool might
# write. Different tools nest the new content differently:
#   Write    -> tool_input.content
#   Edit     -> tool_input.new_string
#   MultiEdit -> tool_input.edits[].new_string
candidate="$(printf '%s' "$payload" | jq -r '
    (.tool_input.content // "") + "\n" +
    (.tool_input.new_string // "") + "\n" +
    ([.tool_input.edits // [] | .[].new_string] | join("\n"))
')"

# Bail early on empty candidates — no point running regex on whitespace.
if [[ -z "$(printf '%s' "$candidate" | tr -d '[:space:]')" ]]; then
    exit 0
fi

# Patterns are loaded from a shared lib so detect-secrets and bash-secret-scan
# stay in lockstep. Add new entries to ``lib/secret-patterns.txt``.
PATTERNS_FILE="$(dirname "$0")/lib/secret-patterns.txt"
if [[ ! -f "$PATTERNS_FILE" ]]; then
    # Without patterns we can't decide; fail open rather than wedge the agent.
    exit 0
fi

matches=()
while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    label="${line%%::*}"
    pattern="${line#*::}"
    # ``-e`` separator stops grep from interpreting patterns starting with ``-``
    # as flags (PEM-style begin/end markers, etc.).
    if printf '%s' "$candidate" | grep -E -q -e "$pattern"; then
        matches+=("$label")
    fi
done <"$PATTERNS_FILE"

if (( ${#matches[@]} == 0 )); then
    exit 0
fi

{
    echo "BLOCKED by detect-secrets hook ($tool_name)."
    echo "The write contained patterns that look like real credentials:"
    for m in "${matches[@]}"; do
        echo "  - $m"
    done
    echo
    echo "If this is a placeholder, paraphrase the value (e.g. sk_live_REDACTED)."
    echo "If you genuinely need to commit a credential, use env vars or the encrypted"
    echo "connections table — never inline."
} >&2
exit 2
