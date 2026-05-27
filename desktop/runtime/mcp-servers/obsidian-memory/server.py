"""obsidian-memory shim — Suite Studio thin wrapper around the vendored
Node.js MCP server at `desktop/runtime/obsidian-memory-mcp/`.

What this file does NOT do
--------------------------
- Implement an MCP server. The protocol implementation lives entirely in
  the vendored TypeScript at `desktop/runtime/obsidian-memory-mcp/`.
- Import any Python equivalent. The vendored server is Node.js (compiled
  from TypeScript); a Python re-implementation would diverge silently
  from upstream — explicitly forbidden by plan non-negotiable #4
  ("DO NOT modify vendored code").

What this file DOES do
----------------------
1. Resolve the operator's Suite Studio vault path from the
   `OBSIDIAN_VAULT_PATH` env var, and re-export it as `MEMORY_DIR` (the
   contract the vendored server expects). The rename is the entire
   reason a shim exists — Suite Studio is namespaced; the vendored
   server is not.
2. Locate the vendored `dist/index.js` and refuse to launch (with a
   structured "run `npm run build`" hint) if it is missing. The
   compiled artifact is `.gitignore`d upstream and must be rebuilt
   locally after every `git submodule update`.
3. `os.execvpe("node", ["node", dist_index_js], env)` so the parent
   process (Hermes Agent's MCP-client transport) keeps the stdio
   JSON-RPC pipes intact and the vendored server inherits them
   directly. No double-buffering, no Python-side proxying of MCP
   messages.

Env contract (set by the sidecar, never by the operator directly)
-----------------------------------------------------------------
    OBSIDIAN_VAULT_PATH  — required; absolute path to the org's vault
                           directory (e.g. ~/SuiteStudio/default/).
    OBSIDIAN_MEMORY_DIST — optional; absolute path to a
                           pre-compiled `dist/index.js`. Defaults to
                           the sibling submodule's `dist/index.js`.
    OBSIDIAN_MEMORY_NODE — optional; override the `node` binary path.
                           Defaults to `"node"` (relies on `$PATH`).

Run command (the sidecar invokes this with `cwd=<this_dir>`)::

    python -m server
"""

from __future__ import annotations

import os
import sys
from typing import Tuple


_HERE = os.path.dirname(os.path.abspath(__file__))

# Walk from `desktop/runtime/mcp-servers/obsidian-memory/` up two levels
# to `desktop/runtime/`, then into the sibling submodule's compiled dist.
_DEFAULT_DIST_INDEX_JS = os.path.abspath(
    os.path.join(_HERE, os.pardir, os.pardir, "obsidian-memory-mcp", "dist", "index.js")
)


def resolve_vault_path() -> str:
    """Resolve the operator's Suite Studio vault path.

    Raises:
        RuntimeError: if ``OBSIDIAN_VAULT_PATH`` is unset, or if the
            referenced directory does not exist. Both are operator
            errors — fail loudly at shim startup rather than let the
            vendored server silently create the vault somewhere
            unexpected.
    """
    vault = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault:
        raise RuntimeError(
            "OBSIDIAN_VAULT_PATH is not set. The sidecar must export it "
            "before spawning this shim. (The vendored Node server reads "
            "MEMORY_DIR; this shim translates from the Suite-Studio-scoped "
            "OBSIDIAN_VAULT_PATH to that.)"
        )
    if not os.path.isdir(vault):
        raise RuntimeError(
            f"OBSIDIAN_VAULT_PATH points at {vault!r}, which is not a directory. "
            "The sidecar's vault-scaffold step is responsible for creating it; "
            "this error means the sidecar skipped that step or the path was "
            "overridden after scaffold."
        )
    return vault


def resolve_dist_index_path() -> str:
    """Return the absolute path to the vendored `dist/index.js`.

    Honors ``OBSIDIAN_MEMORY_DIST`` for tests (and for operators who
    want to swap in a hand-built dist).
    """
    override = os.environ.get("OBSIDIAN_MEMORY_DIST")
    if override:
        return os.path.abspath(override)
    return _DEFAULT_DIST_INDEX_JS


def ensure_dist_built() -> None:
    """Refuse to launch if the vendored TypeScript has not been compiled.

    The vendored repo `.gitignore`s `dist/`, so a fresh
    ``git submodule update`` produces no compiled artifact. The
    operator must run::

        cd desktop/runtime/obsidian-memory-mcp
        npm install
        npm run build

    Raises:
        RuntimeError: with a copy-pasteable runbook in the message
            body. Caught by ``main()`` and printed to stderr before
            exit; the parent process (Hermes Agent) will see the
            non-zero exit and surface the registration failure.
    """
    path = resolve_dist_index_path()
    if not os.path.isfile(path):
        raise RuntimeError(
            f"obsidian-memory dist/index.js not found at {path!r}.\n"
            "Run `npm install && npm run build` inside "
            "desktop/runtime/obsidian-memory-mcp/ to produce it. "
            "(The vendored repo .gitignore's dist/, so this must be done "
            "once per fresh `git submodule update`.)"
        )


def build_exec_args() -> Tuple[list[str], dict[str, str]]:
    """Build the ``(argv, env)`` tuple this shim hands to ``os.execvpe``.

    Validates the vault path and dist-index existence eagerly so a
    misconfigured environment is reported by the Python parent (with a
    helpful message) rather than by the spawned Node process's stderr.

    The returned ``env`` is intentionally lean:
        MEMORY_DIR  — what the vendored server reads.
        PATH        — forwarded so node can find its own runtime.
    Nothing else from the parent environment is forwarded; the
    vendored server has no reason to see Anthropic keys, Hermes Agent
    config, etc.
    """
    vault = resolve_vault_path()
    ensure_dist_built()
    dist = resolve_dist_index_path()
    node = os.environ.get("OBSIDIAN_MEMORY_NODE", "node")
    argv = [node, dist]
    env = {
        "MEMORY_DIR": vault,
        "PATH": os.environ.get("PATH", ""),
    }
    return argv, env


def main() -> int:
    try:
        argv, env = build_exec_args()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    os.execvpe(argv[0], argv, env)
    return 0  # unreachable on success — execvpe replaces the process


if __name__ == "__main__":
    sys.exit(main())
