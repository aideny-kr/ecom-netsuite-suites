import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Connection } from "@/lib/types";

const { mockUseConnections, mockDeleteMutate } = vi.hoisted(() => ({
  mockUseConnections: vi.fn(),
  mockDeleteMutate: vi.fn(async () => undefined),
}));

vi.mock("@/hooks/use-connections", () => ({
  useConnections: mockUseConnections,
  useTestConnection: () => ({ mutateAsync: vi.fn() }),
  useDeleteConnection: () => ({
    mutateAsync: mockDeleteMutate,
    isPending: false,
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/components/add-connection-dialog", () => ({
  AddConnectionDialog: () => null,
}));

vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: { tenant_id: "t-1" } }) }));
vi.mock("@/hooks/use-permissions", () => ({ usePermissions: () => ({ hasPermission: () => true }) }));
vi.mock("@/hooks/use-features", () => ({ useFeature: () => true }));
vi.mock("@/hooks/use-mcp-connectors", () => ({ useMcpConnectors: () => ({ data: [] }), useDeleteMcpConnector: () => ({ mutateAsync: vi.fn() }), useTestMcpConnector: () => ({ mutateAsync: vi.fn() }) }));
vi.mock("@/components/add-mcp-connector-dialog", () => ({ AddMcpConnectorDialog: () => null }));
vi.mock("@/components/settings/celigo-connector-card", () => ({ default: () => <div>Celigo connection management</div> }));
import ConnectionsPage from "../page";

function celigoConnection(over: Partial<Connection> = {}): Connection {
  return {
    id: "c-celigo",
    tenant_id: "t-1",
    provider: "celigo" as Connection["provider"],
    label: "Celigo",
    status: "active",
    auth_type: "api_key",
    credentials_set: true,
    metadata_json: null,
    last_sync_at: null,
    last_health_check_at: null,
    error_reason: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...over,
  };
}

function shopifyConnection(over: Partial<Connection> = {}): Connection {
  return {
    id: "c-shopify",
    tenant_id: "t-1",
    provider: "shopify",
    label: "Shopify",
    status: "active",
    auth_type: "oauth2",
    credentials_set: true,
    metadata_json: null,
    last_sync_at: null,
    last_health_check_at: null,
    error_reason: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  mockUseConnections.mockReset();
  mockDeleteMutate.mockClear();
});

describe("ConnectionsPage — celigo row", () => {
  it("does not present a delete control for a celigo connection", () => {
    mockUseConnections.mockReturnValue({ data: [celigoConnection()], isLoading: false });
    render(<ConnectionsPage />);

    // The only actionable control on a celigo row must not be a button that
    // fires the generic delete mutation (it always 400s server-side).
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("embeds the dedicated Celigo management card", () => {
    mockUseConnections.mockReturnValue({ data: [celigoConnection()], isLoading: false });
    render(<ConnectionsPage />);

    expect(screen.getByText("Celigo connection management")).toBeInTheDocument();
  });

  it("does not duplicate the generic Celigo row", () => {
    mockUseConnections.mockReturnValue({ data: [celigoConnection({ label: "Celigo Prod" })], isLoading: false });
    render(<ConnectionsPage />);

    expect(screen.queryByText("Celigo Prod")).not.toBeInTheDocument();
    expect(screen.getByText("Celigo connection management")).toBeInTheDocument();
  });

  it("leaves the working delete control in place for non-celigo providers", () => {
    mockUseConnections.mockReturnValue({ data: [shopifyConnection()], isLoading: false });
    render(<ConnectionsPage />);

    expect(screen.getByRole("button", { name: "Delete Shopify" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /settings/i })).not.toBeInTheDocument();
  });
});
