// Verbatim subset of frontend/src/lib/types.ts — the interfaces that the reused
// chat-stream.ts normalizer imports. Copied unchanged (NOT reinvented) so the
// reused normalizer ports byte-for-byte and the data_table shape matches the
// webapp's. Future slices widen this as more event types are rendered.

export interface ToolCallTableResultPayload {
  kind: "table";
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  query: string;
  limit: number;
}

export type ToolCallResultPayload = ToolCallTableResultPayload;

export interface ToolCallStep {
  step?: number;
  tool: string;
  params: Record<string, unknown>;
  result_summary: string;
  result_payload?: ToolCallResultPayload | null;
  duration_ms: number;
}

export interface Citation {
  type: "doc" | "table";
  title: string;
  snippet: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  tool_calls: ToolCallStep[] | null;
  citations: Citation[] | null;
  input_tokens?: number;
  output_tokens?: number;
  model_used?: string;
  provider_used?: string;
  is_byok?: boolean;
  confidence_score?: number;
  query_importance?: number; // 1=Casual, 2=Operational, 3=Reporting, 4=Audit Critical
  user_feedback?: "helpful" | "not_helpful" | null;
  structured_output?: { type: string; data: Record<string, unknown> } | null;
  agent_id?: string | null;
  created_at: string;
}

export interface StreamingToolCall {
  tool_name: string;
  tool_input: Record<string, unknown>;
  step: number;
  status: "running" | "complete" | "error";
  duration_ms?: number;
  success?: boolean;
  result_summary?: string;
}

export interface WriteConfirmationData {
  type: "write_confirmation";
  mutation_type: "create" | "update" | "delete" | "upsert";
  record_type: string;
  record_id: string | null;
  proposed_fields: Record<string, unknown>;
  current_record: Record<string, unknown> | null;
  tool_name: string;
  tool_input: Record<string, unknown>;
  confirmation_token: string;
  status: "pending" | "approved" | "rejected";
}

export interface ChartData {
  chart_type: "bar" | "line" | "pie" | "area" | "scatter" | "donut" | "histogram";
  title: string;
  subtitle?: string;
  x_axis: { label: string; key: string };
  y_axes: Array<{ label: string; key: string; color?: string }>;
  data: Record<string, unknown>[];
  options?: {
    stacked?: boolean;
    show_legend?: boolean;
    show_values?: boolean;
    percentage_mode?: boolean;
    sort_by?: string;
    orientation?: "vertical" | "horizontal";
  };
}

export interface ClarificationOption {
  id: "A" | "B" | "C";
  title: string;
  rationale: string;
  source: "netsuite" | "bigquery" | "shopify" | "stripe" | "drive";
  is_default: boolean;
}

export type ClarificationStatus = "pending" | "chosen" | "superseded";

export interface ClarificationData {
  type: "clarification";
  status: ClarificationStatus;
  options: ClarificationOption[];
  default_id: "A" | "B" | "C";
  ambiguity_summary: string;
  confirmation_token: string;
  expires_at: string;
  chosen_id?: "A" | "B" | "C" | null;
  chose_at?: string | null;
}
