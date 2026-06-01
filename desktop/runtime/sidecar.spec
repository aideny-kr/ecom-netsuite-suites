# PyInstaller spec for the Suite Studio Desktop Python sidecar.
#
# Per /goal #5 Phase B (research spike, not packaging perfection):
# bundle the sidecar + vendored Hermes Agent into a self-contained
# directory under `runtime/dist-sidecar/`. The electron-builder config
# at `electron/build/electron-builder.yml` pulls this dist directory
# into the .app bundle's Resources/ at `Resources/sidecar/`.
#
# Why one-folder mode (not --onefile):
#   - Cold-launch latency: one-folder unpacks nothing at startup; the
#     binary spawns in milliseconds. --onefile extracts to /tmp/ on
#     first launch (5-10s) and is the wrong tradeoff for an interactive
#     desktop app where every spawn matters.
#   - Bundle size: one-folder writes plain files (~80MB); --onefile
#     compresses them into the binary (~50MB) but pays decompression
#     latency on every launch.
#   - Code signing: macOS notarization handles a folder of dylibs more
#     cleanly than a single self-extracting binary.
#
# Spike scope notes:
#   - This is the FIRST validation of PyInstaller against the vendored
#     Hermes Agent. Lazy imports in Hermes Agent's provider plugins
#     are flagged in the plan's failure-modes table — if PyInstaller
#     misses them, add explicit `--hidden-import` entries below or
#     fall back to gate #10 OR-branch deferral.
#   - Operator runs: `cd desktop && .venv/bin/pyinstaller runtime/sidecar.spec --noconfirm`
#     followed by `npm run dist` from electron/ to produce the .app.

import os
import sys

HERE = os.path.abspath(os.path.dirname(SPEC))  # noqa: F821 — SPEC is injected by PyInstaller
HERMES_DIR = os.path.join(HERE, "hermes-agent")

# Hermes Agent's `mcp[cli]` package + `anthropic` SDK + `fire` are
# imported via plain `import` at module scope, so PyInstaller's static
# analysis catches them. The hidden imports list below covers the known
# lazy / dynamic imports identified during the OQ-038 packaging spike.
#
# Add more here if `runtime/dist-sidecar/sidecar` fails at runtime with
# ImportError — each one is a Hermes Agent plugin module loaded via
# `importlib` (not `import`), invisible to PyInstaller's AST walk.
HIDDEN_IMPORTS = [
    # Anthropic provider — Hermes Agent's default provider, loaded via
    # `_load_provider("anthropic")` at AIAgent.__init__ time. The
    # plugin's path is `plugins/model-providers/anthropic/__init__.py`
    # within the Hermes Agent submodule.
    "anthropic",
    "anthropic._streaming",
    # Standard MCP transports — the FastMCP client uses these to spawn
    # subprocess-stdio MCP servers (our ns-suiteql + obsidian-memory).
    "mcp",
    "mcp.client",
    "mcp.client.stdio",
    "mcp.server.fastmcp",
    # Hermes Agent's tools surface — sidecar.py calls
    # `tools.mcp_tool.register_mcp_servers`, which dynamically loads
    # the transport adapter.
    "tools.mcp_tool",
]

# The vendored Hermes Agent ships plugins as a data-tree we want copied
# verbatim into the bundle. Without this, lazy `_load_provider` calls
# fail at runtime with FileNotFoundError. Same pattern for the Hermes
# Agent skill packs operators can drop into the runtime.
DATAS = [
    (os.path.join(HERMES_DIR, "plugins"), "plugins"),
    (os.path.join(HERMES_DIR, "tools"), "tools"),
]

block_cipher = None

a = Analysis(  # noqa: F821 — PyInstaller injects this at exec time
    [os.path.join(HERE, "sidecar.py")],
    pathex=[HERE, HERMES_DIR],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy + unused at runtime — pruning these shaves ~15MB off
        # the bundle without losing any feature surface. If a future
        # change wires in test/notebook code paths inside the sidecar,
        # remove the relevant exclude here.
        "tkinter",
        "unittest",
        "pytest",
        "doctest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX compression breaks macOS code-signing — never enable on darwin
    console=True,          # The sidecar IS a console process; Electron pipes its stdio
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,      # PyInstaller picks the host arch; cross-bundle is a /goal #6 concern
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sidecar",
)
