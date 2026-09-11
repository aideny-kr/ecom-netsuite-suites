#!/usr/bin/env bash
# SessionStart + CwdChanged hook.
#
# Prints the current git worktree path, branch name, and whether HEAD diverges
# from origin/main. Counters the "ran tests against the stale main worktree"
# foot-gun documented in memory/feedback_check_stale_worktrees.md and
# memory/feedback_worktree_venv_pth.md.
#
# Output goes to stderr so it shows up in the Claude transcript without being
# parsed as JSON.

set -euo pipefail

if ! command -v git >/dev/null 2>&1; then
    exit 0
fi

# CWD is provided in the JSON payload, but for SessionStart we may not have it;
# fall back to $PWD.
cwd="${PWD:-}"
if [[ -t 0 ]]; then
    :
else
    payload="$(cat)"
    if command -v jq >/dev/null 2>&1; then
        new_cwd="$(printf '%s' "$payload" | jq -r '.cwd // empty')"
        [[ -n "$new_cwd" ]] && cwd="$new_cwd"
    fi
fi

# Bail if we're not inside a git checkout.
if ! git -C "$cwd" rev-parse --show-toplevel >/dev/null 2>&1; then
    exit 0
fi

toplevel="$(git -C "$cwd" rev-parse --show-toplevel)"
branch="$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(detached)')"
worktree_marker=""
if [[ "$toplevel" != "$(git -C "$cwd" rev-parse --git-common-dir | xargs -I {} dirname {} 2>/dev/null || echo $toplevel)" ]]; then
    worktree_marker=" (worktree)"
fi

# Ahead/behind vs origin/main if we can resolve it.
status_line=""
if git -C "$cwd" rev-parse --verify origin/main >/dev/null 2>&1; then
    counts="$(git -C "$cwd" rev-list --left-right --count "origin/main...HEAD" 2>/dev/null || echo "")"
    if [[ -n "$counts" ]]; then
        behind="${counts%%	*}"
        ahead="${counts##*	}"
        status_line=" — ${ahead} ahead, ${behind} behind origin/main"
    fi
fi

# Anthropic hook contract: for SessionStart and CwdChanged, stdout is fed
# back into Claude's context as additional information. stderr is user-only.
# We want Claude itself to see which worktree it's in (the goal is to defeat
# stale-worktree edits), so emit on stdout.
{
    echo "── worktree ──"
    echo "  path:   $toplevel$worktree_marker"
    echo "  branch: $branch$status_line"
}

exit 0
