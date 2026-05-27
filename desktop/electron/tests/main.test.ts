/**
 * Tests for the Electron main-process wiring (electron/main.ts).
 *
 * The Electron `app`, `BrowserWindow`, and `ipcMain` modules are fully
 * mocked so this test never boots a real Electron window — that lives in
 * gate #8 (operator's `npm start` smoke). What we verify here is the
 * SHAPE of the wiring: which lifecycle hooks register what, the
 * `agent:run` IPC contract, and the kill-on-quit guarantee.
 *
 * Plan gates covered: #3 (sidecar lifecycle), #4 (IPC channel), #6
 * (crash propagation to renderer).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { EventEmitter } from "node:events";

// ---------------------------------------------------------------------------
// Mock electron with just enough surface for main.ts to wire against.
// ---------------------------------------------------------------------------

interface FakeApp extends EventEmitter {
  whenReady: ReturnType<typeof vi.fn>;
  on: EventEmitter["on"];
  quit: ReturnType<typeof vi.fn>;
}

interface FakeBrowserWindow {
  loadFile: ReturnType<typeof vi.fn>;
  webContents: { send: ReturnType<typeof vi.fn> };
  on: ReturnType<typeof vi.fn>;
  _opts: unknown;
}

const fakeApp = new EventEmitter() as FakeApp;
fakeApp.whenReady = vi.fn(() => Promise.resolve());
fakeApp.quit = vi.fn();

const browserWindowInstances: FakeBrowserWindow[] = [];
const BrowserWindowMock = vi.fn((opts: unknown) => {
  const w: FakeBrowserWindow = {
    _opts: opts,
    loadFile: vi.fn(),
    webContents: { send: vi.fn() },
    on: vi.fn(),
  };
  browserWindowInstances.push(w);
  return w;
});

const ipcHandlers: Record<string, (...args: unknown[]) => unknown> = {};
const ipcMainMock = {
  handle: vi.fn((channel: string, fn: (...args: unknown[]) => unknown) => {
    ipcHandlers[channel] = fn;
  }),
};

vi.mock("electron", () => ({
  app: fakeApp,
  BrowserWindow: BrowserWindowMock,
  ipcMain: ipcMainMock,
}));

// Mock the Sidecar wrapper so main.ts can be tested without spawning Python
const sidecarStartSpy = vi.fn();
const sidecarKillSpy = vi.fn();
const sidecarRunAgentSpy = vi.fn(async (q: string) => ({ response: `echo: ${q}` }));
const sidecarOnCrashSpy = vi.fn();

vi.mock("../sidecar", () => ({
  Sidecar: vi.fn().mockImplementation(() => ({
    start: sidecarStartSpy,
    kill: sidecarKillSpy,
    runAgent: sidecarRunAgentSpy,
    onCrash: sidecarOnCrashSpy,
  })),
}));

// ---------------------------------------------------------------------------
// Helper to (re-)load main.ts fresh in each test so the lifecycle handlers
// register against a clean event emitter.
// ---------------------------------------------------------------------------

async function loadMain(): Promise<void> {
  // Reset our shared state
  fakeApp.removeAllListeners();
  fakeApp.whenReady = vi.fn(() => Promise.resolve());
  browserWindowInstances.length = 0;
  Object.keys(ipcHandlers).forEach((k) => delete ipcHandlers[k]);
  sidecarStartSpy.mockClear();
  sidecarKillSpy.mockClear();
  sidecarRunAgentSpy.mockClear();
  sidecarOnCrashSpy.mockClear();
  BrowserWindowMock.mockClear();
  ipcMainMock.handle.mockClear();

  // Re-import main.ts — vitest caches module state, so use vi.resetModules()
  vi.resetModules();
  await import("../main");
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Electron main: app.whenReady wiring", () => {
  it("spawns the Sidecar on app ready", async () => {
    await loadMain();
    // whenReady() returned a resolved promise — let it settle
    await Promise.resolve();
    await Promise.resolve();

    expect(sidecarStartSpy).toHaveBeenCalledTimes(1);
  });

  it("creates a BrowserWindow with contextIsolation true and nodeIntegration false", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    expect(BrowserWindowMock).toHaveBeenCalledTimes(1);
    const opts = browserWindowInstances[0]._opts as {
      webPreferences: { contextIsolation: boolean; nodeIntegration: boolean; preload: string };
    };
    expect(opts.webPreferences.contextIsolation).toBe(true);
    expect(opts.webPreferences.nodeIntegration).toBe(false);
    expect(opts.webPreferences.preload).toMatch(/preload\.js$/);
  });

  it("loads renderer.html into the BrowserWindow", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    const w = browserWindowInstances[0];
    expect(w.loadFile).toHaveBeenCalledTimes(1);
    const [arg] = w.loadFile.mock.calls[0];
    expect(arg).toMatch(/renderer\.html$/);
  });
});

describe("Electron main: agent:run IPC contract", () => {
  it("registers an ipcMain handler named 'agent:run'", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    expect(ipcMainMock.handle).toHaveBeenCalledWith("agent:run", expect.any(Function));
    expect(ipcHandlers["agent:run"]).toBeDefined();
  });

  it("agent:run handler delegates to sidecar.runAgent and returns its result", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    // First arg is the IpcMainInvokeEvent (we don't model it here), second
    // is the user query forwarded from preload.
    const fakeEvent = {};
    const result = await ipcHandlers["agent:run"](fakeEvent, "hello sidecar");

    expect(sidecarRunAgentSpy).toHaveBeenCalledWith("hello sidecar");
    expect(result).toEqual({ response: "echo: hello sidecar" });
  });

  it("agent:run handler surfaces sidecar errors instead of throwing across the IPC boundary", async () => {
    sidecarRunAgentSpy.mockResolvedValueOnce({ error: "sidecar crashed" });
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    const result = await ipcHandlers["agent:run"]({}, "anything");
    expect(result).toEqual({ error: "sidecar crashed" });
  });
});

describe("Electron main: before-quit lifecycle", () => {
  it("kills the sidecar on before-quit", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    fakeApp.emit("before-quit");

    expect(sidecarKillSpy).toHaveBeenCalledTimes(1);
  });
});

describe("Electron main: crash propagation (gate #6)", () => {
  it("registers an onCrash handler that forwards the failure to the renderer", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    expect(sidecarOnCrashSpy).toHaveBeenCalledTimes(1);
    const crashHandler = sidecarOnCrashSpy.mock.calls[0][0] as (info: {
      code: number | null;
      signal: string | null;
    }) => void;

    // Simulate the sidecar crashing
    crashHandler({ code: 137, signal: "SIGKILL" });

    const window = browserWindowInstances[0];
    expect(window.webContents.send).toHaveBeenCalledWith(
      "sidecar:crashed",
      expect.objectContaining({ code: 137, signal: "SIGKILL" }),
    );
  });
});
