import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, ApiError } from "@/lib/api-client";
import { createSuiteStudioTools, getWebMcpContext, registerSuiteStudioTools, type WebMcpPageState, type WebMcpTool } from "@/lib/webmcp";

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...original, apiClient: { get: vi.fn() } };
});

describe("Suite Studio WebMCP tools", () => {
  let state: WebMcpPageState;
  let controller: AbortController;
  let tools: WebMcpTool[];
  const get = vi.mocked(apiClient.get);
  const call = (name: string, input: unknown = {}) => tools.find((tool) => tool.name === `suitestudio_${name}`)!.execute(input);

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem("access_token", "test-session-a");
    state = {
      user: { id: "user-a", tenant_id: "tenant-a", tenant_name: "Test A" },
      pathname: "/dashboard", features: { chat: true, workspace: false }, navigate: vi.fn(),
    };
    controller = new AbortController();
    tools = createSuiteStudioTools(() => state, controller.signal);
    get.mockImplementation(async () => state.user);
  });

  it("uses current route state and lists only enabled, implemented destinations", async () => {
    state.pathname = "/connections";
    const result = JSON.parse(await call("get_page_context"));
    expect(result.pathname).toBe("/connections");
    expect(result.tenant.id).toBe("tenant-a");
    const paths = result.navigation_targets.map((target: { path: string }) => target.path);
    expect(paths).toContain("/chat");
    expect(paths).toContain("/tables/orders");
    expect(paths).not.toContain("/workspace");
    expect(paths).not.toContain("/reconciliation");
    expect(paths).toContain("/settings");
    expect(result).not.toHaveProperty("email");
  });

  it.each(["https://evil.example", "//evil.example", "javascript:alert(1)", "/connections?redirect=evil", "/workspace", "/admin/dashboard", "/../settings"])(
    "rejects navigation outside exact available targets: %s", async (path) => {
      await expect(call("navigate", { path })).rejects.toThrow("exact path");
      expect(state.navigate).not.toHaveBeenCalled();
    },
  );

  it("navigates with the router and reports a request rather than claiming arrival", async () => {
    const result = JSON.parse(await call("navigate", { path: "/settings" }));
    expect(state.navigate).toHaveBeenCalledWith("/settings");
    expect(result.status).toBe("navigation_requested");
    expect(JSON.parse(await call("get_page_context")).pathname).toBe("/dashboard");
  });

  it.each([null, [], "", { tenant_id: "tenant-b" }])("rejects malformed or extra input %j", async (input) => {
    await expect(call("get_connection_status", input)).rejects.toThrow("Invalid tool arguments");
    expect(get).not.toHaveBeenCalled();
  });

  it("uses the permission-checked read endpoint and projects only health fields", async () => {
    get.mockResolvedValueOnce(state.user).mockResolvedValueOnce({
      connections: [{ id: "connection-a", label: "Demo", provider: "netsuite", status: "needs_reauth", token_expired: true, tool_count: null, client_id: "private-id", restlet_url: "https://private.example", encrypted_credentials: "secret" }],
      mcp_connectors: [],
    });
    const result = await call("get_connection_status");
    expect(get.mock.calls.map(([path]) => path)).toEqual(["/api/v1/auth/me", "/api/v1/connections/health"]);
    expect(JSON.parse(result).connections[0]).toEqual({ id: "connection-a", label: "Demo", provider: "netsuite", status: "needs_reauth", token_expired: true, tool_count: null });
    expect(result).not.toMatch(/private|secret|test-session/);
  });

  it("preserves permission denial without exposing raw backend detail", async () => {
    get.mockResolvedValueOnce(state.user).mockRejectedValueOnce(new ApiError("private backend detail", 403));
    await expect(call("get_connection_status")).rejects.toThrow("HTTP 403");
  });

  it("refuses to use new-tenant credentials while React still shows the previous tenant", async () => {
    get.mockResolvedValueOnce({ ...state.user, tenant_id: "tenant-b" });
    await expect(call("get_connection_status")).rejects.toThrow("Session changed");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it.each(["token", "user", "abort"])("discards pending health results on %s changes", async (change) => {
    let resolve!: (value: unknown) => void;
    get.mockResolvedValueOnce(state.user).mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
    const pending = call("get_connection_status");
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    if (change === "token") localStorage.setItem("access_token", "test-session-b");
    if (change === "user") state.user = { ...state.user!, tenant_id: "tenant-b" };
    if (change === "abort") controller.abort();
    resolve({ connections: [], mcp_connectors: [] });
    await expect(pending).rejects.toThrow("Session changed");
  });

  it("rejects execution after logout or unmount without a request", async () => {
    state.user = null;
    await expect(call("get_page_context")).rejects.toThrow("Sign in");
    controller.abort();
    await expect(call("navigate", { path: "/settings" })).rejects.toThrow("Sign in");
    expect(get).not.toHaveBeenCalled();
  });

  it("aborts its own registrations on cleanup, including pending registration", async () => {
    const registrations: { tool: WebMcpTool; signal: AbortSignal }[] = [];
    const registerTool = vi.fn((tool: WebMcpTool, options: { signal: AbortSignal }) => {
      registrations.push({ tool, signal: options.signal });
      return Promise.resolve();
    });
    const cleanup = registerSuiteStudioTools({ registerTool }, () => state);
    expect(registrations).toHaveLength(3);
    expect(registrations.every(({ signal }) => !signal.aborted)).toBe(true);
    cleanup();
    expect(registrations.every(({ signal }) => signal.aborted)).toBe(true);
    await expect(registrations[0].tool.execute({})).rejects.toThrow("Sign in");
  });

  it("is unavailable in a browser without the native API", () => {
    expect(getWebMcpContext()).toBeUndefined();
  });
});
