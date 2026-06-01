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
  isPackaged: boolean;
}

interface FakeBrowserWindow {
  loadFile: ReturnType<typeof vi.fn>;
  loadURL: ReturnType<typeof vi.fn>;
  webContents: { send: ReturnType<typeof vi.fn> };
  on: ReturnType<typeof vi.fn>;
  _opts: unknown;
}

const fakeApp = new EventEmitter() as FakeApp;
fakeApp.whenReady = vi.fn(() => Promise.resolve());
fakeApp.quit = vi.fn();
fakeApp.isPackaged = false;

const browserWindowInstances: FakeBrowserWindow[] = [];
const BrowserWindowMock = vi.fn((opts: unknown) => {
  const w: FakeBrowserWindow = {
    _opts: opts,
    loadFile: vi.fn(),
    loadURL: vi.fn(),
    webContents: { send: vi.fn() },
    on: vi.fn(),
  };
  browserWindowInstances.push(w);
  return w;
});

const ipcHandlers: Record<string, (...args: unknown[]) => unknown> = {};
const ipcOnHandlers: Record<string, (...args: unknown[]) => unknown> = {};
const ipcMainMock = {
  handle: vi.fn((channel: string, fn: (...args: unknown[]) => unknown) => {
    ipcHandlers[channel] = fn;
  }),
  on: vi.fn((channel: string, fn: (...args: unknown[]) => unknown) => {
    ipcOnHandlers[channel] = fn;
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
const sidecarRunAgentStreamSpy = vi.fn(
  async (query: string, onEvent: (e: Record<string, unknown>) => void) => {
    onEvent({ type: "text", content: `echo: ${query}` });
    onEvent({ type: "done", tokens_used: 1 });
  },
);
const sidecarOnCrashSpy = vi.fn();

vi.mock("../sidecar", () => ({
  Sidecar: vi.fn().mockImplementation(() => ({
    start: sidecarStartSpy,
    kill: sidecarKillSpy,
    runAgent: sidecarRunAgentSpy,
    runAgentStream: sidecarRunAgentStreamSpy,
    onCrash: sidecarOnCrashSpy,
  })),
}));

// ---------------------------------------------------------------------------
// Helper to (re-)load main.ts fresh in each test so the lifecycle handlers
// register against a clean event emitter.
// ---------------------------------------------------------------------------

async function loadMain(opts: { packaged?: boolean } = {}): Promise<void> {
  // Reset our shared state
  fakeApp.removeAllListeners();
  fakeApp.whenReady = vi.fn(() => Promise.resolve());
  fakeApp.isPackaged = opts.packaged ?? false;
  browserWindowInstances.length = 0;
  Object.keys(ipcHandlers).forEach((k) => delete ipcHandlers[k]);
  Object.keys(ipcOnHandlers).forEach((k) => delete ipcOnHandlers[k]);
  sidecarStartSpy.mockClear();
  sidecarKillSpy.mockClear();
  sidecarRunAgentSpy.mockClear();
  sidecarRunAgentStreamSpy.mockClear();
  sidecarOnCrashSpy.mockClear();
  BrowserWindowMock.mockClear();
  ipcMainMock.handle.mockClear();
  ipcMainMock.on.mockClear();

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

  it("dev (not packaged): loads the renderer via loadURL (dev server), not loadFile", async () => {
    await loadMain({ packaged: false });
    await Promise.resolve();
    await Promise.resolve();

    const w = browserWindowInstances[0];
    expect(w.loadURL).toHaveBeenCalledTimes(1);
    expect(w.loadFile).not.toHaveBeenCalled();
    const [url] = w.loadURL.mock.calls[0];
    expect(String(url)).toMatch(/^https?:\/\//);
  });

  it("packaged: loads the bundled Next static export via loadFile (renderer/index.html)", async () => {
    (process as { resourcesPath?: string }).resourcesPath = "/fake/Resources";
    try {
      await loadMain({ packaged: true });
      await Promise.resolve();
      await Promise.resolve();

      const w = browserWindowInstances[0];
      expect(w.loadFile).toHaveBeenCalledTimes(1);
      expect(w.loadURL).not.toHaveBeenCalled();
      const [arg] = w.loadFile.mock.calls[0];
      expect(String(arg)).toMatch(/renderer[/\\]index\.html$/);
    } finally {
      delete (process as { resourcesPath?: string }).resourcesPath;
    }
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

describe("Electron main: agent:run-stream streaming IPC (rich-pipe)", () => {
  const flush = () => Promise.resolve();

  it("registers an ipcMain.on handler named 'agent:run-stream'", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    expect(ipcMainMock.on).toHaveBeenCalledWith("agent:run-stream", expect.any(Function));
    expect(ipcOnHandlers["agent:run-stream"]).toBeDefined();
  });

  it("forwards each sidecar stream event to the renderer on a per-run channel", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    const sent: Array<[string, unknown]> = [];
    const fakeEvent = { sender: { send: vi.fn((ch: string, ev: unknown) => sent.push([ch, ev])) } };
    ipcOnHandlers["agent:run-stream"](fakeEvent, { runId: "r1", query: "show data" });
    await flush();

    expect(sidecarRunAgentStreamSpy).toHaveBeenCalledWith("show data", expect.any(Function));
    expect(sent.map(([ch]) => ch)).toEqual(["agent:stream:r1", "agent:stream:r1"]);
    expect(sent.map(([, ev]) => (ev as { type: string }).type)).toEqual(["text", "done"]);
  });

  it("does not send to a destroyed renderer sender (guards event.sender.send)", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    // A destroyed WebContents throws synchronously on .send(); the adapter must
    // skip delivery entirely rather than let it throw on the sidecar's stdout
    // handler stack and wedge the single-inflight queue.
    const send = vi.fn();
    const fakeEvent = { sender: { send, isDestroyed: () => true } };
    ipcOnHandlers["agent:run-stream"](fakeEvent, { runId: "r3", query: "show data" });
    await flush();

    // The sidecar stream still ran (two events emitted), but none were sent.
    expect(sidecarRunAgentStreamSpy).toHaveBeenCalledWith("show data", expect.any(Function));
    expect(send).not.toHaveBeenCalled();
  });

  it("rejects a non-string query with an error event instead of running the agent", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    const sent: Array<[string, unknown]> = [];
    const fakeEvent = { sender: { send: vi.fn((ch: string, ev: unknown) => sent.push([ch, ev])) } };
    ipcOnHandlers["agent:run-stream"](fakeEvent, { runId: "r2", query: 123 });
    await flush();

    expect(sidecarRunAgentStreamSpy).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(sent[0][0]).toBe("agent:stream:r2");
    expect((sent[0][1] as { type: string }).type).toBe("error");
  });

  it("rejects a non-string runId with an error event and does not run the agent", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    const sent: Array<[string, unknown]> = [];
    const fakeEvent = { sender: { send: vi.fn((ch: string, ev: unknown) => sent.push([ch, ev])) } };
    ipcOnHandlers["agent:run-stream"](fakeEvent, { runId: 123, query: "a valid query" });
    await flush();

    expect(sidecarRunAgentStreamSpy).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect((sent[0][1] as { type: string }).type).toBe("error");
  });
});

describe("Electron main: agent:run query validation (B0 review MINOR main.ts:83)", () => {
  it("returns an error for a non-string query instead of forwarding it to the sidecar", async () => {
    await loadMain();
    await Promise.resolve();
    await Promise.resolve();

    const result = (await ipcHandlers["agent:run"]({}, { not: "a string" })) as { error?: string };
    expect(result.error).toMatch(/query/i);
    expect(sidecarRunAgentSpy).not.toHaveBeenCalled();
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
