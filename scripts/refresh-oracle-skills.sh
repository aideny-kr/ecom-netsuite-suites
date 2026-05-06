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

echo "==> Refreshing Oracle SuiteCloud SDK agent skills"
npx -y skills add oracle/netsuite-suitecloud-sdk \
  --skill '*' \
  --agent claude-code \
  --copy \
  --yes

echo "==> Cleaning up sibling agent directories created by the skills CLI"
# `npx skills add` creates a directory per supported agent target; we only ship to
# .claude/skills/. The .gitignore already keeps these out of git, but we delete
# them so `ls` stays tidy and contributors don't get confused.
JUNK_DIRS=(
  .adal .agents .aider-desk .augment .bob .codeartsdoer .codebuddy .codemaker
  .codestudio .commandcode .continue .cortex .crush .devin .factory .forge
  .goose .hermes .iflow .junie .kilocode .kiro .kode .mcpjam .mux .neovate
  .openhands .pi .pochi .qoder .qwen .roo .rovodev .tabnine .trae .vibe
  .windsurf .zencoder
)
for d in "${JUNK_DIRS[@]}"; do
  [ -d "$d" ] && rm -rf "$d"
done

# Also remove the root-level skills/ working directory the CLI creates.
[ -d skills ] && rm -rf skills

echo "==> Done. Review the diff:"
echo "    git status --short .claude/skills/ skills-lock.json"
