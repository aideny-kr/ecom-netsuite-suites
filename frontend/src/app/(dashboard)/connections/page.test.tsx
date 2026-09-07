import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import ConnectionsPage from "./page";

const mocks = vi.hoisted(() => ({ manage: true, tenant: "a", remove: vi.fn(), removeMcp: vi.fn(), test: vi.fn(), toast: vi.fn(), fail: false }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: { tenant_id: mocks.tenant } }) }));
vi.mock("@/hooks/use-permissions", () => ({ usePermissions: () => ({ hasPermission: () => mocks.manage }) }));
vi.mock("@/hooks/use-features", () => ({ useFeature: () => true }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: mocks.toast }) }));
vi.mock("@/components/add-connection-dialog", () => ({ AddConnectionDialog: () => <button>Add Connection</button> }));
vi.mock("@/components/add-mcp-connector-dialog", () => ({ AddMcpConnectorDialog: () => <button>Add MCP Connector</button> }));
vi.mock("@/components/settings/celigo-connector-card", () => ({ default: () => <div>Celigo management</div> }));
vi.mock("@/hooks/use-connections", () => ({
  useConnections: () => ({ isError: mocks.fail, refetch: vi.fn(), data: [{ id: "api", provider: "solidus", label: "Framework", status: "active" }, { id: "gone", provider: "api", label: "Removed API", status: "revoked" }, { id: "celigo", provider: "celigo", label: "Celigo duplicate", status: "active" }] }),
  useDeleteConnection: () => ({ mutateAsync: mocks.remove }), useTestConnection: () => ({ mutateAsync: mocks.test }),
}));
vi.mock("@/hooks/use-mcp-connectors", () => ({
  useMcpConnectors: () => ({ data: [{ id: "mcp", provider: "custom", label: "Warehouse MCP", status: "active", server_url: "https://mcp.example/", discovered_tools: [{ name: "orders" }] }] }),
  useDeleteMcpConnector: () => ({ mutateAsync: mocks.removeMcp }), useTestMcpConnector: () => ({ mutateAsync: mocks.test }),
}));
beforeEach(() => { vi.clearAllMocks(); mocks.manage = true; mocks.tenant = "a"; mocks.fail = false; mocks.remove.mockResolvedValue(undefined); mocks.removeMcp.mockResolvedValue(undefined); });
it("shows API and MCP controls and removes each through its own endpoint", async () => {
  render(<ConnectionsPage />);
  expect(screen.getByText("Warehouse MCP")).toBeVisible();
  expect(screen.queryByText("Removed API")).not.toBeInTheDocument();
  expect(screen.queryByText("Celigo duplicate")).not.toBeInTheDocument();
  expect(screen.getByText("Celigo management")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Delete Framework" }));
  expect(mocks.remove).not.toHaveBeenCalled();
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete connection" }));
  await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith("api"));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Delete Warehouse MCP" }));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete connection" }));
  await waitFor(() => expect(mocks.removeMcp).toHaveBeenCalledWith("mcp"));
});
it("hides mutations from viewers", () => {
  mocks.manage = false;
  render(<ConnectionsPage />);
  expect(screen.queryByRole("button", { name: "Add Connection" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add MCP Connector" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Test" })).not.toBeInTheDocument();
});
it("reports delete failures and keeps the dialog available", async () => {
  mocks.remove.mockRejectedValue(new Error("Unavailable"));
  render(<ConnectionsPage />);
  fireEvent.click(screen.getByRole("button", { name: "Delete Framework" }));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete connection" }));
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Could not delete connection" })));
  expect(screen.getByRole("dialog")).toBeVisible();
});
it("discards a pending delete when the tenant changes", () => {
  const view = render(<ConnectionsPage />);
  fireEvent.click(screen.getByRole("button", { name: "Delete Framework" }));
  mocks.tenant = "b";
  view.rerender(<ConnectionsPage />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(mocks.remove).not.toHaveBeenCalled();
});
it("shows a retry when a list fails", () => {
  mocks.fail = true;
  render(<ConnectionsPage />);
  expect(screen.getByRole("alert")).toHaveTextContent("could not be loaded");
  expect(screen.getByRole("button", { name: "Reload connections" })).toBeVisible();
});
