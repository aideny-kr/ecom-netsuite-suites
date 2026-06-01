export {};

// The preload contextBridge surface (desktop/electron/preload.ts) as seen by the
// renderer. Optional because it only exists inside the Electron shell — the
// renderer degrades gracefully when run outside it (e.g. plain `next dev`).
declare global {
  interface SuiteStudioRendererBridge {
    runAgent?(query: string): Promise<{ response?: string; error?: string }>;
    runAgentStream(query: string, onEvent: (event: Record<string, unknown>) => void): void;
    onSidecarCrashed?(cb: (info: { code: number | null; signal: string | null }) => void): void;
  }
  interface Window {
    suiteStudio?: SuiteStudioRendererBridge;
  }
}
