"""CI-safe tests for `desktop/runtime/mcp-servers/obsidian-memory/`.

TDD red phase per /goal #4 plan: failing tests for the obsidian-memory
shim BEFORE writing implementation.

The shim is a thin Python entry point that wraps the vendored Node.js
MCP server at `desktop/runtime/obsidian-memory-mcp/`. We do NOT spawn
real subprocesses in CI — the vendored Node server requires `npm run
build` and a live Node runtime, both of which are operator-deferred.
Instead we cover the shim's responsibilities:

1. Resolves `OBSIDIAN_VAULT_PATH` from the env and hands it to the
   vendored server as `MEMORY_DIR` (the vendored server's contract).
2. Resolves the vendored `dist/index.js` path relative to the
   submodule, with an `OBSIDIAN_MEMORY_DIST` override for tests.
3. Refuses to launch with a structured error if `dist/index.js` is
   missing — the operator must run `npm install && npm run build`
   first. (`OBSIDIAN_MEMORY_NODE` override exists for tests; node
   binary check is best-effort.)
4. Does NOT modify the vendored server's behavior, files, or runtime
   contract — wrapping only, per plan non-negotiable #4.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
_SHIM_PATH = os.path.join(
    HERE, os.pardir, "runtime", "mcp-servers", "obsidian-memory", "server.py"
)

# Load the shim under a unique module name so it does NOT collide with
# ns-suiteql's `server` module (which the pyproject pythonpath already
# exposes for `test_ns_suiteql_server.py`). Both files are legitimately
# named `server.py` because each is invoked as `python -m server` from
# inside its own cwd at runtime — but pytest needs a stable, unique
# importable name to keep them separate in `sys.modules`.
_spec = importlib.util.spec_from_file_location("obsidian_memory_shim", _SHIM_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(
        f"failed to load shim spec from {_SHIM_PATH!r}; "
        "create desktop/runtime/mcp-servers/obsidian-memory/server.py first"
    )
server = importlib.util.module_from_spec(_spec)
sys.modules["obsidian_memory_shim"] = server
_spec.loader.exec_module(server)


# ---------------------------------------------------------------------------
# Vault path resolution
# ---------------------------------------------------------------------------


def test_resolve_vault_path_reads_obsidian_vault_path_env(monkeypatch, tmp_path):
    """The shim must read OBSIDIAN_VAULT_PATH from the env — Suite Studio's
    namespace, not the vendored server's MEMORY_DIR namespace."""
    vault = tmp_path / "SuiteStudio" / "default"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    assert server.resolve_vault_path() == str(vault)


def test_resolve_vault_path_raises_when_env_missing(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.delenv("MEMORY_DIR", raising=False)

    with pytest.raises(RuntimeError) as exc:
        server.resolve_vault_path()

    assert "OBSIDIAN_VAULT_PATH" in str(exc.value), \
        "error must name the Suite-Studio-scoped env var, not the vendored MEMORY_DIR"


def test_resolve_vault_path_raises_when_directory_missing(monkeypatch, tmp_path):
    """A misconfigured path is operator error — fail loudly at shim startup
    rather than silently letting the vendored server create files in the
    wrong place."""
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "does-not-exist"))

    with pytest.raises(RuntimeError) as exc:
        server.resolve_vault_path()

    assert "does-not-exist" in str(exc.value)


# ---------------------------------------------------------------------------
# Vendored entrypoint resolution
# ---------------------------------------------------------------------------


def test_resolve_dist_index_defaults_to_sibling_submodule():
    """With no override, the shim must find the vendored dist/index.js by
    walking up to `desktop/runtime/` and into `obsidian-memory-mcp/dist/`."""
    path = server.resolve_dist_index_path()

    assert path.endswith(os.path.join("obsidian-memory-mcp", "dist", "index.js"))
    assert os.path.isabs(path), f"expected an absolute path, got {path!r}"


def test_resolve_dist_index_honors_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom" / "dist" / "index.js"
    custom.parent.mkdir(parents=True)
    custom.touch()
    monkeypatch.setenv("OBSIDIAN_MEMORY_DIST", str(custom))

    assert server.resolve_dist_index_path() == str(custom)


def test_ensure_dist_built_raises_when_missing(monkeypatch, tmp_path):
    """The shim must surface a clear "run npm run build" hint when the
    vendored TypeScript has not been compiled yet — `dist/index.js` is
    `.gitignore`d in the vendored repo so a fresh submodule clone has
    no dist/."""
    fake_dist = tmp_path / "fake-dist" / "index.js"
    monkeypatch.setenv("OBSIDIAN_MEMORY_DIST", str(fake_dist))

    with pytest.raises(RuntimeError) as exc:
        server.ensure_dist_built()

    msg = str(exc.value)
    assert "npm run build" in msg, f"hint must guide the operator, got: {msg!r}"
    assert "dist/index.js" in msg or "index.js" in msg


def test_ensure_dist_built_succeeds_when_present(monkeypatch, tmp_path):
    fake_dist = tmp_path / "fake-dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.write_text("#!/usr/bin/env node\n")
    monkeypatch.setenv("OBSIDIAN_MEMORY_DIST", str(fake_dist))

    # Should not raise
    server.ensure_dist_built()


# ---------------------------------------------------------------------------
# Subprocess handoff (mocked)
# ---------------------------------------------------------------------------


def test_build_exec_args_passes_node_dist_index(monkeypatch, tmp_path):
    """`build_exec_args` returns the (argv, env) tuple the shim hands to
    `os.execvpe` — must include `node`, the absolute dist/index.js path,
    and `MEMORY_DIR` set to the resolved vault path."""
    vault = tmp_path / "SuiteStudio" / "default"
    vault.mkdir(parents=True)
    dist = tmp_path / "fake-dist" / "index.js"
    dist.parent.mkdir(parents=True)
    dist.write_text("#!/usr/bin/env node\n")

    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("OBSIDIAN_MEMORY_DIST", str(dist))

    argv, env = server.build_exec_args()

    assert argv[0] == os.environ.get("OBSIDIAN_MEMORY_NODE", "node")
    assert argv[1] == str(dist), f"argv must invoke the dist/index.js path, got {argv}"
    assert env["MEMORY_DIR"] == str(vault), \
        "env handed to node must export MEMORY_DIR (the vendored server's contract)"


def test_build_exec_args_passes_path_through(monkeypatch, tmp_path):
    """PATH must be forwarded so the spawned node can find its own
    transitive runtime; nothing else from the parent env leaks
    unintentionally."""
    vault = tmp_path / "SuiteStudio" / "default"
    vault.mkdir(parents=True)
    dist = tmp_path / "fake-dist" / "index.js"
    dist.parent.mkdir(parents=True)
    dist.write_text("#!/usr/bin/env node\n")

    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("OBSIDIAN_MEMORY_DIST", str(dist))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

    argv, env = server.build_exec_args()

    assert env.get("PATH") == "/usr/local/bin:/usr/bin"


def test_build_exec_args_honors_node_binary_override(monkeypatch, tmp_path):
    vault = tmp_path / "SuiteStudio" / "default"
    vault.mkdir(parents=True)
    dist = tmp_path / "fake-dist" / "index.js"
    dist.parent.mkdir(parents=True)
    dist.write_text("#!/usr/bin/env node\n")

    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("OBSIDIAN_MEMORY_DIST", str(dist))
    monkeypatch.setenv("OBSIDIAN_MEMORY_NODE", "/opt/homebrew/bin/node")

    argv, _env = server.build_exec_args()

    assert argv[0] == "/opt/homebrew/bin/node"


# ---------------------------------------------------------------------------
# Vendored-code immutability — sanity check that the shim is wrapping, not
# patching, the vendored server. (Plan non-negotiable #4.)
# ---------------------------------------------------------------------------


def test_shim_does_not_import_from_vendored_module():
    """The shim must not import any module from the vendored Node project
    (it's JavaScript anyway — `from obsidian_memory_mcp import …` would
    silently fail). Asserting absence of such an import statement protects
    against accidental future drift if someone tries to "Pythonize" the
    vendored bits."""
    import inspect
    src = inspect.getsource(server)

    assert "from obsidian_memory_mcp" not in src
    assert "import obsidian_memory_mcp" not in src
