export type JsonObject = Record<string, unknown>;
export interface TransactionConfig {
  id: string;
  tenant_id: string;
  name: string;
  config_key: string;
  source_step_id: string | null;
  source_connection_id?: string | null;
  netsuite_connection_id: string;
  netsuite_account_id: string;
  subsidiary_id: string;
  record_type: string;
  target_step_id: string | null;
  enabled: boolean;
  schedule_enabled: boolean;
  interval_minutes: number;
  max_api_calls: number;
  max_orders: number;
  deadline_seconds: number;
}
export interface TransactionRun {
  id: string;
  tenant_id: string;
  config_id: string;
  origin: string;
  params_json: JsonObject;
  config_snapshot: JsonObject;
  status: string;
  termination_reason: string | null;
  max_api_calls: number;
  max_orders: number;
  api_calls_used: number;
  orders_used: number;
  deadline_at: string;
  progress_json: JsonObject;
  created_at: string;
  finished_at: string | null;
  continuation_run_id?: string | null;
  continuation_blocked?: string | null;
}
export interface TransactionProposal {
  id: string;
  tenant_id: string;
  config_id: string;
  run_id: string;
  work_key: string;
  source_record_id: string;
  order_reference: string;
  target_record_id: string | null;
  action: string;
  currency: string;
  netsuite_account_id: string;
  subsidiary_id: string;
  record_type: string;
  evidence_fingerprint: string;
  observed_at: string;
  valid_until: string;
  before_json: JsonObject;
  after_json: JsonObject;
  evidence_json: JsonObject;
  status: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string;
}
export interface TransactionOperation {
  id: string;
  proposal_id: string;
  status: string;
  attempted_at: string;
  completed_at: string | null;
  result_json: JsonObject;
}
export interface TransactionFinding {
  id: string;
  run_id: string;
  order_reference: string;
  report_json: JsonObject;
  created_at: string;
  updated_at: string;
}
export type RunScope =
  | { order_references: string[] }
  | { window_start: string; window_end: string };
export type DecisionInput = {
  id: string;
  decision: "approve" | "reject";
  evidence_fingerprint: string;
  note?: string;
};
export interface Difference {
  field: string;
  source: string;
  target: string;
  delta: string;
}
