export interface OperationalRun {
  run_id: string;
  origin: string;
  execution_state: string;
  phase: string | null;
  run_state_updated_at: string | null;
  financial_counts: {
    scope: "run_checkpoint";
    matched: number | null;
    needs_review: number | null;
    not_verified: number | null;
  };
  last_read_failure: {
    code: string | null;
    stage: string | null;
    resolved: boolean | null;
    blocking: boolean;
  } | null;
  collection_wait: { basis: string; owner: { status: string } | null } | null;
}

export interface OperationalEntity {
  config_id: string;
  name: string;
  freshness?: {
    state: "healthy" | "paused" | "not_applicable" | "within_grace" | "alert";
    reason: "daily_scan_stopped" | "coverage_overdue" | "daily_completion_pending" | null;
    deadline_at: string | null;
    grace_hours: number;
  };
  coverage: {
    status: string;
    checked_through: string | null;
    expected_checked_through: string | null;
  };
  schedule: { enabled: boolean; kind: string; timezone: string; next_check_at: string | null };
  active_runs: OperationalRun[];
  active_runs_truncated: boolean;
  latest_schedule: OperationalRun | null;
  next_action: { kind: string; reason: string | null; eligible_at: string | null; dispatch_verified: boolean };
}

export interface OperationalStatus {
  observed_at: string;
  source: "stored_reconciliation_state";
  entities: OperationalEntity[];
  truncated: boolean;
  next_offset: number | null;
}
