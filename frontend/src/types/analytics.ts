/**
 * Saved SuiteQL Analytics — API payload and response types.
 *
 * These types mirror the Pydantic schemas defined in
 * backend/app/api/v1/skills.py and must be kept in sync.
 */

// ---------------------------------------------------------------------------
// Saved Query CRUD
// ---------------------------------------------------------------------------

/** POST /api/v1/skills — create a saved query */
export interface SavedQueryCreatePayload {
  name: string;
  description?: string | null;
  query_text: string;
}

/** Response from POST /api/v1/skills */
export interface SavedQueryResponse {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  query_text: string;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Preview
// ---------------------------------------------------------------------------

/** POST /api/v1/skills/preview — request payload */
export interface PreviewRequest {
  query_id: string;
}

/** POST /api/v1/skills/preview — response */
export interface PreviewResponse {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

/** POST /api/v1/skills/export — request payload */
export interface ExportRequest {
  query_id: string;
}

/** POST /api/v1/skills/export — response (202 Accepted) */
export interface ExportResponse {
  task_id: string;
  status: "queued";
}

/** Result returned by Celery task upon completion (polled via job status API) */
export interface ExportTaskResult {
  file_path: string;
  file_name: string;
  row_count: number;
  column_count: number;
  message: string;
}
