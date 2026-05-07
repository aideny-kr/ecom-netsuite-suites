#!/usr/bin/env bash
# Refresh vendored Oracle SuiteCloud SDK agent skills from upstream.
#
# What it does:
#   - Re-runs `npx skills add oracle/netsuite-suitecloud-sdk` against the repo
#   - Updates .claude/skills/netsuite-*/ in place (vendored copies)
#   - Updates skills-lock.json with new hashes
#   - Cleans up junk directories the skills CLI creates as side effects
#
# Usage:
#   ./scripts/refresh-oracle-skills.sh
#
# After running, review the diff and open a PR. We commit `.claude/skills/netsuite-*/`
# and `skills-lock.json` only — see .gitignore for which paths are tracked.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sibling directories that `npx skills add` creates as side effects. We only ship to
# .claude/skills/; everything else is cleanup. The .gitignore already keeps these
# out of git, but we delete them so `ls` stays tidy. Be defensive: only delete
# dirs that DIDN'T exist before this run, so a contributor's unrelated skill-CLI
# state from another invocation isn't blown away.
JUNK_DIRS=(
  .adal .agents .aider-desk .augment .bob .codeartsdoer .codebuddy .codemaker
  .codestudio .commandcode .continue .cortex .crush .devin .factory .forge
  .goose .hermes .iflow .junie .kilocode .kiro .kode .mcpjam .mux .neovate
  .openhands .pi .pochi .qoder .qwen .roo .rovodev .tabnine .trae .vibe
  .windsurf .zencoder
)
PRE_EXISTING=()
for d in "${JUNK_DIRS[@]}" skills; do
  [ -d "$d" ] && PRE_EXISTING+=("$d")
done

echo "==> Refreshing Oracle SuiteCloud SDK agent skills"
npx -y skills add oracle/netsuite-suitecloud-sdk \
  --skill '*' \
  --agent claude-code \
  --copy \
  --yes

echo "==> Cleaning up sibling agent directories created by this run"
_was_preexisting() {
  local target="$1"
  for p in "${PRE_EXISTING[@]:-}"; do
    [ "$p" = "$target" ] && return 0
  done
  return 1
}
for d in "${JUNK_DIRS[@]}" skills; do
  if [ -d "$d" ] && ! _was_preexisting "$d"; then
    rm -rf "$d"
  elif [ -d "$d" ]; then
    echo "    note: leaving pre-existing $d alone"
  fi
done

echo "==> Done. Review the diff:"
echo "    git status --short .claude/skills/ skills-lock.json"
