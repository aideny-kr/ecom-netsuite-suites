import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { apiClient } from "@/lib/api-client";
import { RowDetailDrawer } from "./row-detail-drawer";

vi.mock("@/lib/api-client", () => ({ apiClient: { get: vi.fn() } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("loads only the selected payout through the authenticated API and exposes remaining rows", async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ items: [{ id: "line", line_type: "charge", amount: "12.000", currency: "USD" }], total: 52 });
  render(<RowDetailDrawer open onOpenChange={() => {}} row={{ id: "payout-id" }} tableName="payouts" />);
  expect(await screen.findByText("12.00 USD")).toBeInTheDocument();
  expect(apiClient.get).toHaveBeenCalledWith("/api/v1/tables/payout_lines?payout_id=payout-id&page_size=50");
  expect(screen.getByRole("link", { name: "View all 52 payout lines" })).toHaveAttribute("href", "/tables/payout_lines?payout_id=payout-id");
});

it("shows a read failure instead of claiming no related transactions", async () => {
  vi.mocked(apiClient.get).mockRejectedValue(new Error("unavailable"));
  render(<RowDetailDrawer open onOpenChange={() => {}} row={{ id: "payout-id" }} tableName="payouts" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Payout lines could not be loaded");
  expect(screen.queryByText("No payout lines found.")).not.toBeInTheDocument();
});
