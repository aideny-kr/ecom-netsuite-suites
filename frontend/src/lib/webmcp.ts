import { apiClient, ApiError } from "@/lib/api-client";
import { CANONICAL_TABLES, NAV_ITEMS } from "@/lib/constants";
import type { User } from "@/lib/types";
import type { ConnectionHealthResponse } from "@/hooks/use-connection-health";

// The browser API is still experimental and is not in TypeScript's DOM types.
// Use the native API as a progressive enhancement, without a global polyfill.
export interface WebMcpTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
  execute: (input: unknown) => Promise<string>;
}

export interface WebMcpContext {
  getTools?: () => Promise<Array<{ name: string }>>;
  registerTool: (
    tool: WebMcpTool,
    options: { signal: AbortSignal },
  ) => void | Promise<void>;
}

export interface WebMcpPageState {
  user: Pick<User, "id" | "tenant_id" | "tenant_name"> | null;
  pathname: string;
  features: Record<string, boolean> | undefined;
  navigate: (path: string) => void;
}

export function getWebMcpContext(): WebMcpContext | undefined {
  if (typeof document === "undefined") return;
  const context = (document as Document & { modelContext?: WebMcpContext }).modelContext;
  return typeof context?.registerTool === "function" ? context : undefined;
}

function navigationTargets(features: WebMcpPageState["features"]) {
  return [
    ...NAV_ITEMS.filter(
      (item) => !item.featureFlag || features?.[item.featureFlag] === true,
    ).map(({ label, href }) => ({ label, path: href })),
    ...CANONICAL_TABLES.map(({ name, label }) => ({ label, path: `/tables/${name}` })),
  ];
}

export function validateInput(input: unknown, keys: string[]) {
  if (
    !input || typeof input !== "object" || Array.isArray(input) ||
    Object.keys(input).some((key) => !keys.includes(key))
  ) {
    throw new Error("Invalid tool arguments.");
  }
  return input as Record<string, unknown>;
}

export async function runInWebMcpSession(
  getUser: () => WebMcpPageState["user"],
  signal: AbortSignal,
  action: (assertCurrent: () => void) => unknown | Promise<unknown>,
) {
    const before = getUser();
    if (signal.aborted || !before) throw new Error("Sign in to Suite Studio first.");
    const token = localStorage.getItem("access_token");
    if (!token) throw new Error("Sign in to Suite Studio first.");

    function assertCurrent() {
      const current = getUser();
      if (
        signal.aborted || current?.id !== before!.id ||
        current?.tenant_id !== before!.tenant_id ||
        localStorage.getItem("access_token") !== token
      ) {
        throw new Error("Session changed. Retry after the page finishes loading.");
      }
    }

    try {
      // React can briefly retain tenant A while switchTenant installs tenant B's
      // token. Verify the server identity before reading data or navigating.
      const identity = await apiClient.get<User>("/api/v1/auth/me");
      assertCurrent();
      if (identity.id !== before.id || identity.tenant_id !== before.tenant_id) {
        throw new Error("Session changed. Retry after the page finishes loading.");
      }
      const result = await action(assertCurrent);
      // Discard a late result if logout, refresh, or tenant switching occurred.
      assertCurrent();
      return JSON.stringify(result);
    } catch (error) {
      if (error instanceof ApiError) {
        // Avoid returning raw backend details, which may contain private data.
        throw new Error(`Suite Studio request failed (HTTP ${error.status}).`);
      }
      throw error;
    }
  }


/** Call only through the existing authenticated API. Never accept tenant IDs,
 * URLs, credentials, or arbitrary API paths as tool arguments. */
export function createSuiteStudioTools(
  getState: () => WebMcpPageState,
  signal: AbortSignal,
): WebMcpTool[] {
  const inSession = (action: () => unknown | Promise<unknown>) =>
    runInWebMcpSession(() => getState().user, signal, action);

  const noArguments = { type: "object", properties: {}, additionalProperties: false };
  return [
    {
      name: "suitestudio_get_page_context",
      description: "Read the current Suite Studio page, active tenant, and available navigation targets. Does not read page contents or URL query parameters.",
      inputSchema: noArguments,
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: async (input) => {
        validateInput(input, []);
        return inSession(async () => {
          const state = getState();
          const tools = await getWebMcpContext()?.getTools?.() || [];
          return {
            pathname: state.pathname,
            title: document.title,
            origin: window.location.origin,
            frontend_mode: process.env.NODE_ENV,
            webmcp_version: 1,
            available_tools: tools.map((tool) => tool.name).filter((name) => name.startsWith("suitestudio_")),
            tenant: { id: state.user!.tenant_id, name: state.user!.tenant_name },
            navigation_targets: navigationTargets(state.features),
          };
        });
      },
    },
    {
      name: "suitestudio_navigate",
      description: "Open a Suite Studio section using an exact path returned by suitestudio_get_page_context. Changes the visible page. Returns a navigation request; call get_page_context to verify arrival. Does not submit forms or run workflows.",
      inputSchema: {
        type: "object", properties: { path: { type: "string", description: "An exact navigation_targets path." } },
        required: ["path"], additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      execute: async (input) => {
        const { path } = validateInput(input, ["path"]);
        return inSession(() => {
          const state = getState();
          if (typeof path !== "string" || !navigationTargets(state.features).some((target) => target.path === path)) {
            throw new Error("Choose an exact path from navigation_targets.");
          }
          state.navigate(path);
          return { requested_path: path, status: "navigation_requested" };
        });
      },
    },
    {
      name: "suitestudio_get_connection_status",
      description: "Read stored connection health for the signed-in tenant. Requires connections.view permission. Reports stored status and credential expiry; does not contact providers or prove live connectivity. Omits credentials and connection URLs.",
      inputSchema: noArguments,
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: async (input) => {
        validateInput(input, []);
        return inSession(async () => {
          const health = await apiClient.get<ConnectionHealthResponse>("/api/v1/connections/health");
          const summarize = (items: ConnectionHealthResponse["connections"]) => items.map((item) => ({
            id: item.id, label: item.label, provider: item.provider,
            status: item.status, token_expired: item.token_expired,
            tool_count: item.tool_count,
          }));
          return { connections: summarize(health.connections), mcp_connectors: summarize(health.mcp_connectors) };
        });
      },
    },
  ];
}

/** AbortSignal scopes registrations and pending executions to this component's
 * authenticated lifetime. Never clear tools owned by another component. */
export function registerSuiteStudioTools(context: WebMcpContext, getState: () => WebMcpPageState) {
  const controller = new AbortController();
  for (const tool of createSuiteStudioTools(getState, controller.signal)) {
    try {
      Promise.resolve(context.registerTool(tool, { signal: controller.signal })).catch(() => {
        if (!controller.signal.aborted) console.warn(`WebMCP could not register ${tool.name}.`);
      });
    } catch {
      console.warn(`WebMCP could not register ${tool.name}.`);
    }
  }
  return () => controller.abort();
}
