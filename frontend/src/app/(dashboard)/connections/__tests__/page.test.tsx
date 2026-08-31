import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Connection } from "@/lib/types";

const { mockUseConnections, mockDeleteMutate } = vi.hoisted(() => ({
  mockUseConnections: vi.fn(),
  mockDeleteMutate: vi.fn(async () => undefined),
}));

vi.mock("@/hooks/use-connections", () => ({
  useConnections: mockUseConnections,
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

  it("points to Settings as where the celigo connection is managed", () => {
    mockUseConnections.mockReturnValue({ data: [celigoConnection()], isLoading: false });
    render(<ConnectionsPage />);

    const link = screen.getByRole("link", { name: /settings/i });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("still shows the celigo row itself rather than hiding it", () => {
    mockUseConnections.mockReturnValue({ data: [celigoConnection({ label: "Celigo Prod" })], isLoading: false });
    render(<ConnectionsPage />);

    expect(screen.getByText("Celigo Prod")).toBeInTheDocument();
  });

  it("leaves the working delete control in place for non-celigo providers", () => {
    mockUseConnections.mockReturnValue({ data: [shopifyConnection()], isLoading: false });
    render(<ConnectionsPage />);

    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("link", { name: /settings/i })).not.toBeInTheDocument();
  });
});
