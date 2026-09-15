import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { apiClient } from "@/lib/api-client";
import { TableToolbar } from "./table-toolbar";

vi.mock("@/lib/api-client", () => ({ apiClient: { getText: vi.fn() } }));
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("access_token", "test-token");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ blob: async () => new Blob() }));
  URL.createObjectURL = vi.fn().mockReturnValue("blob:export");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("exports the displayed filters through the authenticated API client", async () => {
  vi.mocked(apiClient.getText).mockResolvedValue("order_number,total_amount\nR100120031,1724.00");
  render(<TableToolbar tableName="orders" search="R100" filters={{ currency: "USD" }} onSearchChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
  await waitFor(() => expect(apiClient.getText).toHaveBeenCalledWith("/api/v1/tables/orders/export/csv?currency=USD&search=R100"));
  expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
});

it("shows export failures and never downloads the error response", async () => {
  vi.mocked(apiClient.getText).mockRejectedValue(new Error("Export exceeds 10,000 rows. Please narrow your filters."));
  render(<TableToolbar tableName="orders" search="" onSearchChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("narrow your filters");
  expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
});
