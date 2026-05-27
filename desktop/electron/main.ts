/**
 * Electron main process for Suite Studio Desktop (B0 spike, /goal #5).
 *
 * Lifecycle (per plan gates #3, #4, #6):
 *
 *   app.whenReady ──► spawn Sidecar (Python --serve)
 *                  └► create BrowserWindow (CSP-locked: contextIsolation
 *                     true, nodeIntegration false, sandbox true, preload
 *                     bridge only)
 *                  └► load renderer.html
 *                  └► wire Sidecar onCrash → webContents.send
 *                     'sidecar:crashed'  (gate #6)
 *
 *   ipcMain.handle('agent:run')
 *      └► await sidecar.runAgent(query) → returns {response} or {error}
 *
 *   app.before-quit ──► sidecar.kill()
 *
 * Resolution rules for paths (dev vs packaged):
 *
 *   - In dev (npm start) the Python interpreter is the operator's
 *     desktop/.venv (where the vendored Hermes Agent is installed editable).
 *   - In packaged builds (npm run dist) the Python sidecar is the
 *     PyInstaller-bundled binary shipped as an extraResource at
 *     `<resourcesPath>/sidecar/sidecar`. Gate #10 — may be deferred via
 *     OR-branch with a fall-back to system `python3`.
 */
import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";
import { Sidecar, type AgentResult } from "./sidecar";

let mainWindow: BrowserWindow | null = null;
let sidecar: Sidecar | null = null;

function resolvePythonPath(): string {
  if (app.isPackaged) {
    // PyInstaller emits a self-contained binary; spawning it directly is
    // the same shape as spawning python — the Sidecar wrapper treats
    // sidecarPath as the script for python or as the entrypoint for the
    // PyInstaller binary depending on context.
    return path.join(process.resourcesPath, "sidecar", "sidecar");
  }
  // Dev: rely on the operator's desktop/.venv. If the .venv is missing
  // we fall back to system python3 so `npm start` at least runs the
  // scaffold, but the agent will fail at `import run_agent` and that
  // failure surfaces clearly via the JSON-line `{"error":...}` path.
  return path.resolve(__dirname, "..", "..", ".venv", "bin", "python");
}

function resolveSidecarPath(): string {
  if (app.isPackaged) {
    // PyInstaller bundle is the entrypoint itself; no separate .py needed.
    // Passing empty string means the Sidecar wrapper invokes the binary
    // with `["--serve"]` only (no script path).
    return "";
  }
  return path.resolve(__dirname, "..", "..", "runtime", "sidecar.py");
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 900,
    height: 700,
    title: "Suite Studio Desktop v0 — spike",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  win.loadFile(path.join(__dirname, "..", "renderer.html"));
  return win;
}

// IPC handler is registered at module-load time so it survives across
// window recreation (macOS pattern: re-open window on activate after
// all windows closed). The handler delegates to the live Sidecar
// instance; if the sidecar isn't running yet (or has died) we return a
// structured error instead of throwing across the IPC boundary —
// renderer surfaces the error to the user.
ipcMain.handle("agent:run", async (_event, query: string): Promise<AgentResult> => {
  if (!sidecar) {
    return { error: "sidecar not yet ready" };
  }
  try {
    return await sidecar.runAgent(query);
  } catch (err) {
    return { error: (err as Error).message };
  }
});

app.whenReady().then(() => {
  sidecar = new Sidecar({
    pythonPath: resolvePythonPath(),
    sidecarPath: resolveSidecarPath(),
  });
  sidecar.start();

  mainWindow = createWindow();

  sidecar.onCrash((info) => {
    if (mainWindow && !mainWindow.isDestroyed?.()) {
      mainWindow.webContents.send("sidecar:crashed", info);
    }
  });
});

app.on("before-quit", () => {
  sidecar?.kill();
});

// On macOS it's common to keep the app alive after all windows close.
// On other platforms we quit — the sidecar is killed via before-quit.
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
