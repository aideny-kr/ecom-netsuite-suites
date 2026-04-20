import { apiClient } from "@/lib/api-client";

// ---------- Types ----------

export type RunKind = "benchmark" | "experiment";
export type RunMode = "all" | "single";
export type RunStatus = "running" | "completed" | "cancelled" | "failed";

export interface BenchmarkCaseResult {
  case_id: string;
  ours: BenchmarkSideResult | null;
  mcp: BenchmarkSideResult | null;
}

export interface BenchmarkSideResult {
  side: "ours" | "mcp";
  accuracy: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  success: boolean | null;
  answer_preview: string | null;
  error_message: string | null;
}

export interface ExperimentCaseResult {
  case_id: string;
  dialect: "suiteql" | "bigquery";
  decision: "KEEP" | "REVERT" | "SKIP";
  experiment_score: number;
  generated_sql: string | null;
  executed_successfully: boolean | null;
  error_message: string | null;
  cost_usd: number;
}

export type CaseResult = BenchmarkCaseResult | ExperimentCaseResult;

export interface AgentLabRun {
  run_id: string;
  kind: RunKind;
  mode: RunMode;
  status: RunStatus;
  total_cases: number;
  cases_completed: number;
  cost_usd_actual: number;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
}

export interface RunSnapshot {
  run: AgentLabRun;
  cases: CaseResult[];
}

export interface PatternRow {
  id: string;
  user_question: string;
  working_sql: string;
  tables_used: string[] | null;
  success_count: number;
  last_used_at: string | null;
  created_at: string | null;
}

// ---------- SSE event types ----------

export type AgentLabEvent =
  | { type: "preparing"; phase: "generating" | "mining" | "loading"; detail?: string }
  | { type: "run_started"; total_cases: number; estimated_cost_usd: number }
  | { type: "case_started"; case_id: string; question: string; index: number }
  | {
      type: "case_complete";
      case_id: string;
      result: CaseResult;
      running_cost_usd: number;
      cases_completed: number;
    }
  | {
      type: "run_complete";
      status: "completed" | "cancelled" | "failed";
      summary?: Record<string, unknown>;
      total_cost_usd: number;
    }
  | { type: "heartbeat"; ts: number };

// ---------- API functions ----------

export async function startRun(params: {
  kind: RunKind;
  mode: RunMode;
  case_id?: string;
}): Promise<{ run_id: string; status: string }> {
  return apiClient.post("/api/v1/agent-lab/runs", params);
}

export async function listRuns(params: {
  kind?: RunKind;
  days?: number;
}): Promise<AgentLabRun[]> {
  const q = new URLSearchParams();
  if (params.kind) q.set("kind", params.kind);
  if (params.days) q.set("days", String(params.days));
  return apiClient.get(`/api/v1/agent-lab/runs?${q.toString()}`);
}

export async function getRunSnapshot(runId: string): Promise<RunSnapshot> {
  return apiClient.get(`/api/v1/agent-lab/runs/${runId}`);
}

export async function cancelRun(runId: string): Promise<{ cancelled: boolean }> {
  return apiClient.post(`/api/v1/agent-lab/runs/${runId}/cancel`, {});
}

export async function listPatterns(): Promise<PatternRow[]> {
  return apiClient.get("/api/v1/agent-lab/patterns");
}
