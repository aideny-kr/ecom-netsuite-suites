#!/usr/bin/env bash
#
# Build helper for the polyglot Phase B packaging spike (/goal #5).
#
# Run this BEFORE `npm run dist` to stage the two non-Electron runtimes
# that the .app bundle needs:
#
#   1. PyInstaller-bundled Python sidecar at ../../runtime/dist-sidecar/
#      → electron-builder picks this up as extraResource `Resources/sidecar/`
#   2. Standalone Node runtime at ./node-runtime/bin/node
#      → electron-builder picks this up as extraResource `Resources/node-runtime/`
#      → main.ts prepends `Resources/node-runtime/bin` to the sidecar's PATH
#        so the obsidian-memory shim's `node` lookup finds the bundled binary
#
# Strategy choices (per plan pre-flight defaults):
#   - Python bundling: (a) PyInstaller — mature, ~80MB output, no
#     onefile compression to avoid cold-launch decompression latency
#   - Node bundling:   (a) sibling Node binary via extraResources —
#     simpler than pkg/nexe compilation, more honest about runtime
#     cost than deferring (option c) entirely
#
# Both are OR-branch eligible per plan gates #9 + #10 — if either step
# below fails on the operator's machine, document the failure mode in
# ../../SMOKE-DEFERRAL-ELECTRON-LAUNCH.md and skip the corresponding
# extraResources entry in build/electron-builder.yml.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ELECTRON_DIR="$(cd "$HERE/.." && pwd)"
DESKTOP_DIR="$(cd "$ELECTRON_DIR/.." && pwd)"
NODE_VERSION="${NODE_VERSION:-v20.18.0}"  # LTS; matches our package.json @types/node major
ARCH="${ARCH:-$(uname -m)}"
if [[ "$ARCH" == "x86_64" ]]; then ARCH="x64"; fi
PLATFORM="${PLATFORM:-darwin}"

echo "==> Suite Studio Desktop polyglot bundle build"
echo "    Desktop dir:  $DESKTOP_DIR"
echo "    Electron dir: $ELECTRON_DIR"
echo "    Node:         $NODE_VERSION $PLATFORM-$ARCH"
echo

# ---------------------------------------------------------------------
# Step 1: PyInstaller bundle (gate #10)
# ---------------------------------------------------------------------
echo "==> [1/3] PyInstaller-bundling the Python sidecar"
if [[ ! -x "$DESKTOP_DIR/.venv/bin/pyinstaller" ]]; then
  echo "    Installing pyinstaller into desktop/.venv ..."
  "$DESKTOP_DIR/.venv/bin/pip" install pyinstaller
fi

"$DESKTOP_DIR/.venv/bin/pyinstaller" \
  "$DESKTOP_DIR/runtime/sidecar.spec" \
  --noconfirm \
  --distpath "$DESKTOP_DIR/runtime/dist-sidecar" \
  --workpath "$DESKTOP_DIR/runtime/build-sidecar"

SIDECAR_BUNDLE_SIZE="$(du -sh "$DESKTOP_DIR/runtime/dist-sidecar/sidecar" 2>/dev/null | awk '{print $1}' || echo "?")"
echo "    Bundle size: $SIDECAR_BUNDLE_SIZE"
echo

# ---------------------------------------------------------------------
# Step 2: Standalone Node runtime download (gate #9)
# ---------------------------------------------------------------------
echo "==> [2/3] Downloading standalone Node $NODE_VERSION for $PLATFORM-$ARCH"
NODE_DIR="$ELECTRON_DIR/build/node-runtime"
NODE_ARCHIVE="$NODE_DIR/node-$NODE_VERSION-$PLATFORM-$ARCH.tar.gz"
NODE_URL="https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-$PLATFORM-$ARCH.tar.gz"

if [[ -x "$NODE_DIR/bin/node" ]]; then
  echo "    Node already present at $NODE_DIR/bin/node — skipping download."
else
  mkdir -p "$NODE_DIR"
  echo "    Downloading from $NODE_URL ..."
  curl -fsSL -o "$NODE_ARCHIVE" "$NODE_URL"
  echo "    Extracting ..."
  tar -xzf "$NODE_ARCHIVE" -C "$NODE_DIR" --strip-components=1
  rm -f "$NODE_ARCHIVE"
fi

NODE_RUNTIME_SIZE="$(du -sh "$NODE_DIR" 2>/dev/null | awk '{print $1}' || echo "?")"
echo "    Runtime size: $NODE_RUNTIME_SIZE"
echo

# ---------------------------------------------------------------------
# Step 3: Build the vendored Obsidian Memory MCP if not already built
# ---------------------------------------------------------------------
echo "==> [3/3] Compiling vendored obsidian-memory-mcp (if needed)"
MCP_DIR="$DESKTOP_DIR/runtime/obsidian-memory-mcp"
if [[ -f "$MCP_DIR/dist/index.js" ]]; then
  echo "    dist/index.js already present — skipping."
else
  if [[ ! -d "$MCP_DIR/node_modules" ]]; then
    ( cd "$MCP_DIR" && npm install )
  fi
  ( cd "$MCP_DIR" && npm run build )
fi
echo

echo "==> Polyglot bundle ready."
echo "    Run: cd $ELECTRON_DIR && npm run dist"
