/**
 * Renderer↔main bridge for Suite Studio Desktop.
 *
 * The renderer has no Node access (nodeIntegration:false,
 * contextIsolation:true). The ONLY surface it sees is
 * `window.suiteStudio.runAgent` and `window.suiteStudio.onSidecarCrashed`,
 * exposed here via contextBridge.
 *
 * No additional functions are exposed for the B0 spike — every new
 * capability the renderer needs must be added explicitly here, which is
 * the point of contextBridge: every exposed surface is auditable.
 */
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("suiteStudio", {
  runAgent: (query: string) => ipcRenderer.invoke("agent:run", query),
  onSidecarCrashed: (
    cb: (info: { code: number | null; signal: string | null }) => void,
  ) => {
    ipcRenderer.on("sidecar:crashed", (_event, info) => cb(info));
  },
});
