import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  test: vi.fn(),
  connect: vi.fn(),
}));

vi.mock("@/hooks/use-celigo", () => ({
  useCeligoStatus: () => ({ data: mocks.status(), isLoading: false }),
  useCeligoTest: () => ({ mutateAsync: mocks.test, isPending: false }),
  useCeligoConnect: () => ({ mutateAsync: mocks.connect, isPending: false }),
  useCeligoDisconnect: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

// Real usePermissions() returns { hasPermission, isAdmin, permissions } — the
// brief's draft mocked a `has()` method that does not exist on the real hook.
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

describe("CeligoConnectorCard", () => {
  it("warns against personal access tokens", async () => {
    mocks.status.mockReturnValue({ connected: false });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);
    expect(screen.getByText(/service token/i)).toBeInTheDocument();
    expect(screen.getByText(/90 days/i)).toBeInTheDocument();
  });

  it("offers both regions", async () => {
    mocks.status.mockReturnValue({ connected: false });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);
    expect(screen.getByRole("option", { name: /united states/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /europe/i })).toBeInTheDocument();
  });

  it("submits the token and region on connect", async () => {
    mocks.status.mockReturnValue({ connected: false });
    mocks.connect.mockResolvedValue({ connected: true });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);

    fireEvent.change(screen.getByLabelText(/api token/i), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));

    await waitFor(() =>
      expect(mocks.connect).toHaveBeenCalledWith(
        expect.objectContaining({ token: "s3cret", region: "us" }),
      ),
    );
  });

  it("shows the account name once connected", async () => {
    mocks.status.mockReturnValue({ connected: true, account_name: "Framework", region: "us" });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);
    expect(screen.getByText("Framework")).toBeInTheDocument();
  });

  it("never labels the connection as able to change anything", async () => {
    mocks.status.mockReturnValue({ connected: true, account_name: "Framework", region: "us" });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
  });

  it("submits the agent token on connect when provided", async () => {
    mocks.status.mockReturnValue({ connected: false });
    mocks.connect.mockResolvedValue({ connected: true, agent_access: true });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);

    fireEvent.change(screen.getByLabelText(/api token/i), { target: { value: "s3cret" } });
    fireEvent.change(screen.getByLabelText(/agent access/i), { target: { value: "agent-tok" } });
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));

    await waitFor(() =>
      expect(mocks.connect).toHaveBeenCalledWith(
        expect.objectContaining({ token: "s3cret", region: "us", agent_token: "agent-tok" }),
      ),
    );
  });

  it("omits agent_token from connect when the field is left blank", async () => {
    mocks.status.mockReturnValue({ connected: false });
    mocks.connect.mockResolvedValue({ connected: true });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);

    fireEvent.change(screen.getByLabelText(/api token/i), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));

    await waitFor(() => expect(mocks.connect).toHaveBeenCalled());
    const payload = mocks.connect.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.agent_token).toBeUndefined();
  });

  it("shows agent access is enabled once connected with an agent token", async () => {
    mocks.status.mockReturnValue({
      connected: true,
      account_name: "Framework",
      region: "us",
      agent_access: true,
    });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);
    expect(screen.getByText(/agent access enabled/i)).toBeInTheDocument();
  });

  it("shows agent access is not enabled once connected without an agent token", async () => {
    mocks.status.mockReturnValue({
      connected: true,
      account_name: "Framework",
      region: "us",
      agent_access: false,
    });
    const { default: Card } = await import("../celigo-connector-card");
    wrap(<Card />);
    expect(screen.getByText(/agent access.*not enabled/i)).toBeInTheDocument();
  });
});
