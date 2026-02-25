export interface Tenant {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  config: TenantConfig | null;
  created_at: string;
  updated_at: string;
}

export interface TenantConfig {
  id: string;
  tenant_id: string;
  netsuite_subsidiary_id: string | null;
  netsuite_location_id: string | null;
  netsuite_currency: string;
  sync_frequency_minutes: number;
  auto_post_to_netsuite: boolean;
  ai_provider: string | null;
  ai_model: string | null;
  ai_api_key_set: boolean;
  created_at: string;
  updated_at: string;
}

export interface User {
  id: string;
  tenant_id: string;
  tenant_name: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  onboarding_completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export type Role = "owner" | "admin" | "member" | "viewer";

export interface Connection {
  id: string;
  tenant_id: string;
  provider: "shopify" | "stripe" | "netsuite";
  label: string;
  status: "active" | "inactive" | "error" | "revoked";
  auth_type: string | null;
  credentials_set: boolean;
  metadata_json: Record<string, unknown> | null;
  last_sync_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEvent {
  id: number;
  tenant_id: string;
  timestamp: string;
  actor_id: string | null;
  actor_type: string;
  category: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  correlation_id: string | null;
  payload: Record<string, unknown> | null;
  status: string;
  error_message: string | null;
}

export interface Job {
  id: string;
  tenant_id: string;
  job_type: string;
  status: "pending" | "running" | "completed" | "failed";
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Order {
  id: string;
  tenant_id: string;
  connection_id: string;
  external_id: string;
  order_number: string;
  status: string;
  currency: string;
  total_amount: number;
  subtotal_amount: number;
  tax_amount: number;
  discount_amount: number;
  shipping_amount: number;
  customer_email: string | null;
  customer_name: string | null;
  order_date: string;
  raw_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface Payment {
  id: string;
  tenant_id: string;
  connection_id: string;
  external_id: string;
  order_id: string | null;
  amount: number;
  currency: string;
  status: string;
  payment_method: string | null;
  payment_date: string;
  raw_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface Refund {
  id: string;
  tenant_id: string;
  connection_id: string;
  external_id: string;
  order_id: string | null;
  payment_id: string | null;
  amount: number;
  currency: string;
  status: string;
  reason: string | null;
  refund_date: string;
  raw_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface Payout {
  id: string;
  tenant_id: string;
  connection_id: string;
  external_id: string;
  amount: number;
  currency: string;
  status: string;
  payout_date: string;
  arrival_date: string | null;
  raw_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface PayoutLine {
  id: string;
  tenant_id: string;
  payout_id: string;
  type: string;
  source_id: string | null;
  amount: number;
  currency: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface Dispute {
  id: string;
  tenant_id: string;
  connection_id: string;
  external_id: string;
  payment_id: string | null;
  amount: number;
  currency: string;
  status: string;
  reason: string | null;
  due_date: string | null;
  raw_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface NetsuitePosting {
  id: string;
  tenant_id: string;
  entity_type: string;
  entity_id: string;
  netsuite_id: string | null;
  posting_type: string;
  status: "pending" | "posted" | "failed" | "skipped";
  amount: number;
  currency: string;
  error: string | null;
  posted_at: string | null;
  raw_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface PlanLimits {
  max_connections: number;
  max_schedules: number;
  max_exports_per_day: number;
  mcp_tools: boolean;
  chat: boolean;
  byok_ai: boolean;
}

export interface PlanUsage {
  connections: number;
  schedules: number;
}

export interface PlanInfo {
  plan: string;
  limits: PlanLimits;
  usage: PlanUsage;
  plan_expires_at: string | null;
}

export interface McpConnector {
  id: string;
  tenant_id: string;
  provider: "netsuite_mcp" | "shopify_mcp" | "stripe_mcp" | "custom";
  label: string;
  server_url: string;
  auth_type: "bearer" | "api_key" | "none" | "oauth2";
  status: string;
  discovered_tools: McpDiscoveredTool[] | null;
  is_enabled: boolean;
  encryption_key_version: number;
  metadata_json: Record<string, unknown> | null;
  created_at: string;
  created_by: string | null;
}

export interface McpDiscoveredTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown> | null;
}

export interface McpConnectorTestResponse {
  connector_id: string;
  status: string;
  message: string;
  discovered_tools: McpDiscoveredTool[] | null;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface TenantSummary {
  id: string;
  name: string;
  slug: string;
  plan: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  tenant_name: string;
  tenant_slug: string;
  email: string;
  password: string;
  full_name: string;
}

export interface AuthResponse {
  access_token: string;
  refresh_token?: string;
  token_type: string;
  user?: User;
}

export interface ChatSession {
  id: string;
  title: string | null;
  workspace_id?: string | null;
  session_type?: string;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ToolCallStep {
  step?: number;
  tool: string;
  params: Record<string, unknown>;
  result_summary: string;
  duration_ms: number;
}

export interface ProposePatchResult {
  changeset_id: string;
  patch_id: string;
  operation: "modify" | "create" | "delete";
  diff_status: string;
  risk_summary: string;
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
  created_at: string;
}

export interface ChatSessionDetail {
  id: string;
  title: string | null;
  workspace_id?: string | null;
  is_archived: boolean;
  messages: ChatMessage[];
  created_at: string;
  updated_at: string;
}

// --- Workspace Types ---

export interface Workspace {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceFile {
  id: string;
  workspace_id: string;
  path: string;
  file_name: string;
  mime_type: string | null;
  size_bytes: number;
  is_directory: boolean;
  created_at: string;
  updated_at: string;
}

export interface FileTreeNode {
  id: string;
  name: string;
  path: string;
  is_directory: boolean;
  children?: FileTreeNode[];
  size_bytes?: number;
}

export interface FileReadResponse {
  id: string;
  path: string;
  file_name: string;
  content: string;
  truncated: boolean;
  total_lines: number;
  mime_type: string | null;
}

export interface SearchResult {
  file_id: string;
  path: string;
  line_number: number;
  snippet: string;
  context: string;
}

export interface ChangeSet {
  id: string;
  workspace_id: string;
  title: string;
  description: string | null;
  status: "draft" | "pending_review" | "approved" | "applied" | "rejected";
  proposed_by: string;
  reviewed_by: string | null;
  applied_by: string | null;
  proposed_at: string;
  reviewed_at: string | null;
  applied_at: string | null;
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
  patches?: Patch[];
}

export interface Patch {
  id: string;
  changeset_id: string;
  file_path: string;
  operation: "modify" | "create" | "delete";
  unified_diff: string | null;
  new_content: string | null;
  baseline_sha256: string;
  apply_order: number;
  created_at: string;
}

export interface DiffViewResponse {
  changeset_id: string;
  title: string;
  files: Array<{
    file_path: string;
    operation: string;
    original_content: string;
    modified_content: string;
  }>;
}

// --- Workspace Run Types ---

export type RunType =
  | "sdf_validate"
  | "jest_unit_test"
  | "suiteql_assertions"
  | "deploy_sandbox";

export type RunStatus = "queued" | "running" | "passed" | "failed" | "error";

export interface WorkspaceRun {
  id: string;
  workspace_id: string;
  changeset_id: string | null;
  run_type: RunType;
  status: RunStatus;
  command: string | null;
  exit_code: number | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceArtifact {
  id: string;
  run_id: string;
  artifact_type: "stdout" | "stderr" | "report_json" | "coverage_json";
  content: string | null;
  size_bytes: number;
  sha256_hash: string | null;
  created_at: string;
}

// --- Onboarding Wizard Types ---

export interface OnboardingChecklistItem {
  step_key: string;
  status: "pending" | "completed" | "skipped";
  completed_at: string | null;
  completed_by: string | null;
  metadata: Record<string, unknown> | null;
}

export interface OnboardingChecklist {
  items: OnboardingChecklistItem[];
  all_completed: boolean;
  finalized_at: string | null;
}

export interface StepValidationResponse {
  step_key: string;
  valid: boolean;
  reason?: string;
}

export interface OnboardingAuditEvent {
  id: number;
  action: string;
  created_at: string;
  correlation_id: string | null;
  actor_id: string | null;
  resource_type: string | null;
  resource_id: string | null;
  payload: Record<string, unknown> | null;
}

export interface OnboardingAuditTrailResponse {
  events: OnboardingAuditEvent[];
}

// --- SuiteQL Assertions ---

export interface AssertionExpected {
  type: "row_count" | "scalar" | "no_rows";
  operator: "eq" | "ne" | "gt" | "gte" | "lt" | "lte" | "between";
  value?: number;
  value2?: number;
}

export interface AssertionDefinition {
  name: string;
  query: string;
  expected: AssertionExpected;
  notes?: string;
  tags?: string[];
}

export interface AssertionResult {
  name: string;
  query: string;
  expected: AssertionExpected;
  status: "passed" | "failed" | "error";
  actual_value?: number;
  row_count?: number;
  error?: string;
  duration_ms: number;
}

export interface AssertionsReport {
  run_id: string;
  tenant_id: string;
  timestamp: string;
  assertions: AssertionResult[];
  summary: {
    total: number;
    passed: number;
    failed: number;
    errors: number;
  };
  overall_status: "passed" | "failed";
  total_duration_ms: number;
}

// --- UAT Report ---

export interface UATReport {
  changeset_id: string;
  changeset_title: string;
  changeset_status: string;
  gates: {
    validate: string;
    unit_tests: string;
    assertions: string;
    deploy: string;
  };
  runs: Array<{
    run_type: string;
    run_id: string;
    status: string;
    duration_ms: number | null;
    started_at: string | null;
    completed_at: string | null;
  }>;
  assertions_report: AssertionsReport | null;
  overall_status: string;
  generated_at: string;
}

// --- Deploy Sandbox ---

export interface DeployGates {
  validate: { status: string; run_id: string | null };
  unit_tests: { status: string; run_id: string | null };
  assertions: { status: string; run_id: string | null; skipped?: boolean };
}

// --- NetSuite Metadata Discovery Types ---

export interface NetSuiteMetadataCategories {
  transaction_body_fields: number;
  transaction_column_fields: number;
  entity_custom_fields: number;
  item_custom_fields: number;
  custom_record_types: number;
  custom_lists: number;
  subsidiaries: number;
  departments: number;
  classifications: number;
  locations: number;
}

export interface NetSuiteMetadata {
  id: string;
  version: number;
  status: "pending" | "completed" | "failed" | "not_discovered";
  message?: string;
  discovered_at: string | null;
  total_fields_discovered: number;
  queries_succeeded: number;
  discovery_errors: Record<string, string> | null;
  categories: NetSuiteMetadataCategories;
}

export interface NetSuiteMetadataFieldItem {
  scriptid?: string;
  label?: string;
  fieldtype?: string;
  description?: string;
  ismandatory?: string;
  id?: string;
  name?: string;
  isinactive?: string;
  parent?: string;
}

export interface NetSuiteMetadataFieldsResponse {
  category: string;
  count: number;
  data: NetSuiteMetadataFieldItem[];
}

export interface MetadataDiscoveryTaskResponse {
  task_id: string;
  status: string;
}

// --- Admin / Billing Types ---

export interface AdminWallet {
  tenant_id: string;
  stripe_customer_id: string | null;
  stripe_subscription_item_id: string | null;
  billing_period_start: string;
  billing_period_end: string;
  base_credits_remaining: number;
  metered_credits_used: number;
  last_synced_metered_credits: number;
}

export interface AdminTenant {
  id: string;
  name: string;
  slug: string;
  plan: string;
  is_active: boolean;
  created_at: string;
  user_count: number;
  wallet: AdminWallet | null;
}

export interface PlatformStats {
  active_tenants: number;
  total_tenants: number;
  total_users: number;
  total_base_credits_remaining: number;
  total_metered_credits_used: number;
}

export interface ImpersonateResponse {
  access_token: string;
  token_type: string;
  tenant_id: string;
  tenant_name: string;
}
