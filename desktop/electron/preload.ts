/**
 * Renderer↔main bridge for Suite Studio Desktop.
 *
 * The renderer has no Node access (nodeIntegration:false,
 * contextIsolation:true). The ONLY surface it sees is `window.suiteStudio`,
 * exposed here via contextBridge. Every new capability must be added
 * explicitly here — that auditability is the point of contextBridge.
 *
 * Surfaces:
 *   runAgent(query)             — single-shot request/response (back-compat).
 *   runAgentStream(query, on)   — rich-pipe streaming: opens a per-run IPC
 *                                 event channel, forwards each typed event to
 *                                 `onEvent`, and unsubscribes on the terminal
 *                                 done/error.
 *   onSidecarCrashed(cb)        — sidecar crash notifications.
 */
import { contextBridge, ipcRenderer } from "electron";

let runCounter = 0;

contextBridge.exposeInMainWorld("suiteStudio", {
  runAgent: (query: string) => ipcRenderer.invoke("agent:run", query),

  runAgentStream: (query: string, onEvent: (event: Record<string, unknown>) => void) => {
    // Unique per-run channel so concurrent streams never cross-talk. (Renderer
    // context: Date.now()/counter are fine here — not the headless sandbox.)
    const runId = `run-${++runCounter}-${Date.now()}`;
    const channel = `agent:stream:${runId}`;
    const listener = (_event: unknown, payload: Record<string, unknown>) => {
      onEvent(payload);
      const type = (payload as { type?: unknown }).type;
      if (type === "done" || type === "error") {
        ipcRenderer.removeListener(channel, listener);
      }
    };
    ipcRenderer.on(channel, listener);
    ipcRenderer.send("agent:run-stream", { runId, query });
  },

  onSidecarCrashed: (
    cb: (info: { code: number | null; signal: string | null }) => void,
  ) => {
    ipcRenderer.on("sidecar:crashed", (_event, info) => cb(info));
  },
});
