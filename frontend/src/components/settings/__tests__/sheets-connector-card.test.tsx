import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SheetsConnectorCard } from "../sheets-connector-card";

vi.mock("@/hooks/use-permissions", () => ({
  usePermissions: () => ({ isAdmin: true }),
}));

const mockTestMutate = vi.fn(async (payload: unknown) => ({ valid: true, error: null }));
const mockCreateMutate = vi.fn(async () => ({}));

vi.mock("@/hooks/use-mcp-connectors", () => ({
  useMcpConnectors: () => ({ data: [] }),
  useDeleteMcpConnector: () => ({ mutateAsync: vi.fn() }),
  useTestSheetsConnection: () => ({
    mutateAsync: mockTestMutate,
    isPending: false,
  }),
  useCreateSheetsConnector: () => ({
    mutateAsync: mockCreateMutate,
    isPending: false,
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

const VALID_SA_JSON = JSON.stringify({
  type: "service_account",
  client_email: "sa@x.iam.gserviceaccount.com",
  private_key: "pk",
});

describe("SheetsConnectorCard — Shared Drive input", () => {
  beforeEach(() => {
    mockTestMutate.mockClear();
    mockCreateMutate.mockClear();
  });

  it("renders the Shared Drive ID input in not-connected state", () => {
    render(wrap(<SheetsConnectorCard />));
    expect(
      screen.getByLabelText(/shared drive id/i),
    ).toBeInTheDocument();
  });

  it("passes shared_drive_id to test mutation when populated", async () => {
    render(wrap(<SheetsConnectorCard />));
    fireEvent.change(screen.getByPlaceholderText(/service account json/i), {
      target: { value: VALID_SA_JSON },
    });
    fireEvent.change(screen.getByLabelText(/shared drive id/i), {
      target: { value: "0ACabcdEFGH1234567890" },
    });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    await waitFor(() => expect(mockTestMutate).toHaveBeenCalled());
    expect(mockTestMutate.mock.calls[0][0]).toMatchObject({
      shared_drive_id: "0ACabcdEFGH1234567890",
    });
  });

  it("omits shared_drive_id from test mutation when input is empty", async () => {
    render(wrap(<SheetsConnectorCard />));
    fireEvent.change(screen.getByPlaceholderText(/service account json/i), {
      target: { value: VALID_SA_JSON },
    });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    await waitFor(() => expect(mockTestMutate).toHaveBeenCalled());
    const payload = mockTestMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.shared_drive_id).toBeUndefined();
  });

  it("shows validation error for malformed Shared Drive ID", async () => {
    render(wrap(<SheetsConnectorCard />));
    fireEvent.change(screen.getByPlaceholderText(/service account json/i), {
      target: { value: VALID_SA_JSON },
    });
    fireEvent.change(screen.getByLabelText(/shared drive id/i), {
      target: { value: "short" },
    });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    await waitFor(() =>
      expect(screen.getByText(/invalid shared drive id/i)).toBeInTheDocument(),
    );
    expect(mockTestMutate).not.toHaveBeenCalled();
  });
});
