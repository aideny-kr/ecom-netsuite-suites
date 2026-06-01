// Ambient types shared across main / preload / renderer.
//
// Lives at file scope (no `declare module` / `declare global`) so any
// .ts file in this project picks these up without an explicit import.

interface AgentResult {
  response?: string;
  error?: string;
}

// One typed streaming event forwarded from the sidecar (rich-pipe slice 1).
// The bridge stays schema-agnostic; the renderer's chat-stream.ts normalizer
// validates the concrete shapes (text / data_table / done / error).
type AgentStreamEvent = Record<string, unknown>;

interface SuiteStudioBridge {
  runAgent(query: string): Promise<AgentResult>;
  runAgentStream(query: string, onEvent: (event: AgentStreamEvent) => void): void;
  onSidecarCrashed?(cb: (info: { code: number | null; signal: string | null }) => void): void;
}

interface Window {
  suiteStudio: SuiteStudioBridge;
}
