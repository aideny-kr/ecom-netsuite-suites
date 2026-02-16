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
  created_at: string;
  updated_at: string;
}

export type Role = "owner" | "admin" | "member" | "viewer";

export interface Connection {
  id: string;
  tenant_id: string;
  provider: "shopify" | "stripe" | "netsuite";
  label: string;
  status: "active" | "inactive" | "error";
  credentials_set: boolean;
  last_sync_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEvent {
  id: string;
  tenant_id: string;
  user_id: string | null;
  category: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  detail: Record<string, unknown> | null;
  correlation_id: string | null;
  created_at: string;
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

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
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
  refresh_token: string;
  token_type: string;
  user: User;
}
