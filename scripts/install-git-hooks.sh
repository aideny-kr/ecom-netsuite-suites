#!/usr/bin/env bash
# Install git hooks that keep the code index fresh.
#
# These are GIT hooks, not Claude hooks, and the distinction is load-bearing: the
# index goes stale on branch switch and merge, which are git events that happen
# whether or not an agent is running. A Claude hook cannot see them, so wiring this
# to PostToolUse would leave the index confidently describing the branch you just
# left — and a stale index is worse than none, because its answers look structured.
#
# Idempotent. Refuses to clobber an unrelated existing hook.
#
#   ./scripts/install-git-hooks.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS="$(git -C "$ROOT" rev-parse --git-common-dir)/hooks"
mkdir -p "$HOOKS"

MARK="# managed-by: scripts/install-git-hooks.sh"

install_one() {
  local name="$1" target="$HOOKS/$1"
  if [[ -e "$target" ]] && ! grep -q "$MARK" "$target" 2>/dev/null; then
    echo "  SKIP $name — exists and was not installed by this script; merge by hand"
    return
  fi
  cat > "$target" <<EOF
#!/usr/bin/env bash
$MARK
# Reindex only what changed. Runs in the CURRENT worktree, which is the one whose
# index just went stale. Backgrounded and silenced so a slow or broken index can
# never block a checkout — the index is an accelerator, not a gate.
if [[ -x "\$(git rev-parse --show-toplevel)/scripts/codegraph.py" ]]; then
  ( "\$(git rev-parse --show-toplevel)/scripts/codegraph.py" index >/dev/null 2>&1 & ) || true
fi
exit 0
EOF
  chmod +x "$target"
  echo "  ok   $name"
}

echo "installing into $HOOKS"
# post-checkout covers branch switch AND new-worktree creation; post-merge covers
# pull and merge; post-rewrite covers rebase, which silently rewrites the tree.
for h in post-checkout post-merge post-rewrite; do install_one "$h"; done
echo
echo "Verify with:  git checkout -q \$(git branch --show-current) && sleep 1 && ./scripts/codegraph.py stats"
