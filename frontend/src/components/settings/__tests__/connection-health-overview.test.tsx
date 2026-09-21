import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConnectionOverview } from "../connection-overview";

const mocks = vi.hoisted(() => ({ remove: vi.fn(), get: vi.fn().mockResolvedValue({ uses: [{ name: "Stock report", href: "/scheduled-jobs/job1", binding: "exact binding", active: true }], visibility_limited: true, coverage: "Supported bindings only." }) }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: { tenant_id: "a" } }) }));
vi.mock("@/hooks/use-permissions", () => ({ usePermissions: () => ({ hasPermission: () => true }) }));
vi.mock("@/hooks/use-features", () => ({ useFeature: () => false }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("@/lib/api-client", () => ({ apiClient: { get: mocks.get } }));
vi.mock("@/components/add-connection-dialog", () => ({ AddConnectionDialog: () => null }));
vi.mock("@/components/add-mcp-connector-dialog", () => ({ AddMcpConnectorDialog: () => null }));
vi.mock("@/hooks/use-connections", () => ({
  useConnections: () => ({ data: [{ id: "api1", label: "ERP API", provider: "netsuite", status: "active" }] }),
  useDeleteConnection: () => ({ mutateAsync: mocks.remove }), useTestConnection: () => ({ mutateAsync: vi.fn() }),
}));
vi.mock("@/hooks/use-mcp-connectors", () => ({
  useMcpConnectors: () => ({ data: [{ id: "mcp1", label: "ERP tools", provider: "netsuite_mcp", status: "active", server_url: "https://example.test/mcp" }] }),
  useDeleteMcpConnector: () => ({ mutateAsync: mocks.remove }), useTestMcpConnector: () => ({ mutateAsync: vi.fn() }),
}));
vi.mock("@/hooks/use-connection-health", () => ({ useConnectionHealth: () => ({ data: {
  connections: [{ id: "api1", status: "active", verification_status: "ok", last_health_check: "2026-09-01T00:00:00Z", account_identity: "sandbox", access_scope: "rest_webservices", role: "Reader" }],
  mcp_connectors: [{ id: "mcp1", status: "needs_reauth", last_health_check: null }],
} }) }));

describe("per-method health and disconnect", () => {
  it("groups one system while preserving its failed method and exact setup target", () => {
    render(<ConnectionOverview setup={{ netsuite: <p>Existing setup</p> }} />);
    expect(screen.getAllByRole("heading", { name: "NetSuite" })).toHaveLength(1);
    expect(screen.getByText("Verified at last test")).toBeVisible();
    expect(screen.getByText("Authorization expired")).toBeVisible();
    expect(screen.getByText("sandbox")).toBeVisible();
    expect(screen.getByText("rest_webservices · Reader")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "Connection setup" })[1]).toHaveAttribute("href", "#connection-settings-mcp-mcp1");
    expect(screen.getByText("No recorded check")).toBeVisible();
  });
  it("shows actual dependencies before any delete and cancellation performs no write", async () => {
    render(<ConnectionOverview />);
    fireEvent.click(screen.getByRole("button", { name: "Delete ERP API" }));
    expect(await screen.findByRole("link", { name: "Stock report" })).toBeVisible();
    expect(mocks.get).toHaveBeenCalledWith("/api/v1/connections/usage/api/api1");
    expect(mocks.remove).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(mocks.remove).not.toHaveBeenCalled();
  });
});
