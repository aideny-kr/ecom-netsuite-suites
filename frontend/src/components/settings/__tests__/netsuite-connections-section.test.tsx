import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { McpConnector } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  connections: vi.fn(),
  mcpConnectors: vi.fn(),
  health: vi.fn(),
}));

vi.mock("@/hooks/use-connection-health", () => ({
  useConnectionHealth: () => ({ data: mocks.health() }),
}));

vi.mock("@/hooks/use-connections", () => ({
  useConnections: () => ({ data: mocks.connections() }),
  useDeleteConnection: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useReconnectConnection: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTestConnection: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateClientId: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateRestletUrl: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/use-mcp-connectors", () => ({
  useMcpConnectors: () => ({ data: mocks.mcpConnectors() }),
  useDeleteMcpConnector: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useReauthorizeMcpConnector: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTestMcpConnector: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateMcpClientId: () => ({ mutate: vi.fn(), isPending: false }),
}));

// Real usePermissions() pulls from useAuth(); short-circuit with the shape the
// component actually reads (isAdmin) — same pattern as celigo-connector-card.test.tsx.
vi.mock("@/hooks/use-permissions", () => ({
  usePermissions: () => ({
    hasPermission: () => true,
    isAdmin: true,
    permissions: new Set<string>(),
  }),
}));

vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function mcpConnector(overrides: Partial<McpConnector>): McpConnector {
  return {
    id: "id",
    tenant_id: "tenant",
    provider: "netsuite_mcp",
    label: "MCP",
    server_url: "https://example.com",
    auth_type: "oauth2",
    status: "active",
    discovered_tools: null,
    is_enabled: true,
    encryption_key_version: 1,
    metadata_json: null,
    last_health_check_at: null,
    error_reason: null,
    created_at: "2026-01-01T00:00:00Z",
    created_by: null,
    ...overrides,
  };
}

describe("NetSuiteConnectionsSection — MCP connector filtering", () => {
  it("excludes a celigo_mcp connector from the MCP Tool Connections list and never selects it as active", async () => {
    mocks.connections.mockReturnValue([]);
    mocks.health.mockReturnValue(undefined);
    // API orders by created_at DESC, so a tenant that connected Celigo AFTER
    // NetSuite gets it back first. A denylist filter (status !== "revoked" &&
    // provider !== "bigquery") lets celigo_mcp through, and
    // `mcpConns.find(active) ?? mcpConns[0]` then picks IT as activeMcp --
    // editing "Client ID" in this NetSuite section would silently PATCH the
    // Celigo connector's credentials instead.
    mocks.mcpConnectors.mockReturnValue([
      mcpConnector({
        id: "celigo-1",
        provider: "celigo_mcp",
        label: "Celigo (agent access)",
        auth_type: "bearer",
        created_at: "2026-02-01T00:00:00Z",
        metadata_json: { client_id: "celigo-should-not-leak" },
      }),
      mcpConnector({
        id: "ns-1",
        provider: "netsuite_mcp",
        label: "NetSuite MCP",
        created_at: "2026-01-01T00:00:00Z",
        metadata_json: { client_id: "ns-real-client-id" },
      }),
    ]);

    const { NetSuiteConnectionsSection } = await import("../netsuite-connections-section");
    wrap(<NetSuiteConnectionsSection />);

    expect(screen.queryByText("Celigo (agent access)")).not.toBeInTheDocument();
    expect(screen.getByText("NetSuite MCP")).toBeInTheDocument();

    // activeMcp must be the NetSuite row, not Celigo -- proven via the MCP
    // "Client ID" field, which is derived from activeMcp.metadata_json.client_id.
    expect(screen.getByText("ns-real-client-id")).toBeInTheDocument();
    expect(screen.queryByText("celigo-should-not-leak")).not.toBeInTheDocument();
  });

  it("still shows a netsuite_mcp connector when it is the only one configured", async () => {
    mocks.connections.mockReturnValue([]);
    mocks.health.mockReturnValue(undefined);
    mocks.mcpConnectors.mockReturnValue([
      mcpConnector({ id: "ns-1", provider: "netsuite_mcp", label: "NetSuite MCP" }),
    ]);

    const { NetSuiteConnectionsSection } = await import("../netsuite-connections-section");
    wrap(<NetSuiteConnectionsSection />);

    expect(screen.getByText("NetSuite MCP")).toBeInTheDocument();
  });
});
