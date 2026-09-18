#!/usr/bin/env bash
# ConfigChange hook — append-only audit log of harness mutations.
#
# Why: in a multi-developer / multi-agent repo, silent changes to settings,
# skills, agents, hooks, and .mcp.json change the deterministic-enforcement
# surface. We never want the agent (or anyone) to quietly loosen this. The
# audit log is in the worktree's .git/.. so it's per-checkout, not committed.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

payload="$(cat)"

# Resolve a writable log dir — prefer the git-toplevel, fall back to $PWD.
toplevel="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
log_dir="$toplevel/.claude/audit"
mkdir -p "$log_dir" 2>/dev/null || exit 0
log_file="$log_dir/config-changes.log"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Anthropic ConfigChange payload shape (per official docs as of Q1 2026):
#   { hook_event_name, source, file_path, change_type, ... }
# We log the canonical fields plus a "raw" passthrough so anything we missed
# is still captured (and we don't blow up if the schema evolves).
summary="$(printf '%s' "$payload" | jq -c '{
    hook_event: (.hook_event_name // "ConfigChange"),
    source: (.source // null),
    file_path: (.file_path // null),
    change_type: (.change_type // null),
    raw: .
}')"

printf '%s %s\n' "$ts" "$summary" >> "$log_file"
exit 0
