#!/usr/bin/env bash
# PostToolUse hook for Write | Edit | MultiEdit.
#
# Runs a formatter scoped to the file extension Claude just touched. Keeps
# diffs small (no whole-repo reformat) and surfaces formatter output to the
# agent via stderr so it learns to keep style stable.
#
# Anthropic hook contract: this runs AFTER the edit succeeds, so exit 2
# only feeds context back — it does not roll the edit back.
#
# Trade-off taken: we only format the single touched file, and we never
# auto-fail on style errors here — that's CI's job. The goal is "keep the
# agent's diffs free of trivial style churn," not "block the turn."

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // ""')"

case "$tool_name" in
    Write|Edit|MultiEdit) ;;
    *) exit 0 ;;
esac

# Tool-input file_path is always populated for these three tools.
file_path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty')"
[[ -z "$file_path" ]] && exit 0
[[ ! -f "$file_path" ]] && exit 0

# Resolve which directory to run the formatter from. We expect a backend/
# or frontend/ ancestor; otherwise skip.
ext="${file_path##*.}"
project_root=""
if [[ "$file_path" == */backend/* ]]; then
    project_root="${file_path%%/backend/*}/backend"
elif [[ "$file_path" == */frontend/* ]]; then
    project_root="${file_path%%/frontend/*}/frontend"
fi

case "$ext" in
    py)
        # Ruff is the canonical Python formatter for this repo. Run if available.
        ruff="${project_root}/.venv/bin/ruff"
        if [[ -x "$ruff" ]]; then
            "$ruff" format "$file_path" >/dev/null 2>&1 || true
        elif command -v ruff >/dev/null 2>&1; then
            ruff format "$file_path" >/dev/null 2>&1 || true
        fi
        ;;
    ts|tsx|js|jsx)
        # Prefer prettier when wired up via project deps; otherwise no-op.
        if [[ -n "$project_root" && -x "$project_root/node_modules/.bin/prettier" ]]; then
            "$project_root/node_modules/.bin/prettier" --write "$file_path" >/dev/null 2>&1 || true
        fi
        ;;
    json|yaml|yml|md)
        # Skip — too much risk of formatting comments / multi-doc YAML wrong.
        ;;
esac

exit 0
