// Ambient types shared across main / preload / renderer.
//
// Lives at file scope (no `declare module` / `declare global`) so any
// .ts file in this project picks these up without an explicit import.

interface AgentResult {
  response?: string;
  error?: string;
}

interface SuiteStudioBridge {
  runAgent(query: string): Promise<AgentResult>;
  onSidecarCrashed?(cb: (info: { code: number | null; signal: string | null }) => void): void;
}

interface Window {
  suiteStudio: SuiteStudioBridge;
}
