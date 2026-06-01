/**
 * Tests for the preload contextBridge (electron/preload.ts).
 *
 * The renderer's ONLY surface is `window.suiteStudio`. The rich-pipe slice adds
 * `runAgentStream(query, onEvent)`: it opens a per-run IPC event channel
 * (ipcRenderer.on) rather than a single invoke, sends a run-stream request to
 * main, forwards each event to onEvent, and unsubscribes on the terminal
 * done/error. `electron` is mocked so no real Electron runs.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";

const exposed: Record<string, any> = {};
const contextBridgeMock = {
  exposeInMainWorld: vi.fn((key: string, api: unknown) => {
    exposed[key] = api;
  }),
};
const ipcRendererMock = {
  invoke: vi.fn(async () => ({ response: "ok" })),
  on: vi.fn(),
  removeListener: vi.fn(),
  send: vi.fn(),
};

vi.mock("electron", () => ({
  contextBridge: contextBridgeMock,
  ipcRenderer: ipcRendererMock,
}));

async function loadPreload(): Promise<void> {
  Object.keys(exposed).forEach((k) => delete exposed[k]);
  contextBridgeMock.exposeInMainWorld.mockClear();
  ipcRendererMock.invoke.mockClear();
  ipcRendererMock.on.mockClear();
  ipcRendererMock.removeListener.mockClear();
  ipcRendererMock.send.mockClear();
  vi.resetModules();
  await import("../preload");
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("preload contextBridge", () => {
  it("exposes suiteStudio with runAgent, runAgentStream, and onSidecarCrashed", async () => {
    await loadPreload();
    expect(contextBridgeMock.exposeInMainWorld).toHaveBeenCalledWith("suiteStudio", expect.any(Object));
    expect(typeof exposed.suiteStudio.runAgent).toBe("function");
    expect(typeof exposed.suiteStudio.runAgentStream).toBe("function");
    expect(typeof exposed.suiteStudio.onSidecarCrashed).toBe("function");
  });

  it("runAgentStream sends a run-stream request and subscribes to a per-run channel", async () => {
    await loadPreload();
    exposed.suiteStudio.runAgentStream("show data", () => {});

    expect(ipcRendererMock.send).toHaveBeenCalledTimes(1);
    const [sendChannel, payload] = ipcRendererMock.send.mock.calls[0] as [string, { runId: string; query: string }];
    expect(sendChannel).toBe("agent:run-stream");
    expect(payload.query).toBe("show data");
    expect(typeof payload.runId).toBe("string");
    expect(payload.runId.length).toBeGreaterThan(0);

    expect(ipcRendererMock.on).toHaveBeenCalledWith(`agent:stream:${payload.runId}`, expect.any(Function));
  });

  it("forwards each event to onEvent and unsubscribes on the terminal done", async () => {
    await loadPreload();
    const events: Array<Record<string, unknown>> = [];
    exposed.suiteStudio.runAgentStream("q", (e: Record<string, unknown>) => events.push(e));

    const [, payload] = ipcRendererMock.send.mock.calls[0] as [string, { runId: string }];
    const channel = `agent:stream:${payload.runId}`;
    const listener = (ipcRendererMock.on.mock.calls.find((c) => c[0] === channel) as [string, Function])[1];

    listener({}, { type: "text", content: "hi" });
    listener({}, { type: "done", tokens_used: 1 });

    expect(events.map((e) => e.type)).toEqual(["text", "done"]);
    expect(ipcRendererMock.removeListener).toHaveBeenCalledWith(channel, listener);
  });

  it("unsubscribes on a terminal error event too", async () => {
    await loadPreload();
    exposed.suiteStudio.runAgentStream("q", () => {});

    const [, payload] = ipcRendererMock.send.mock.calls[0] as [string, { runId: string }];
    const channel = `agent:stream:${payload.runId}`;
    const listener = (ipcRendererMock.on.mock.calls.find((c) => c[0] === channel) as [string, Function])[1];

    listener({}, { type: "error", error: "boom" });

    expect(ipcRendererMock.removeListener).toHaveBeenCalledWith(channel, listener);
  });
});
