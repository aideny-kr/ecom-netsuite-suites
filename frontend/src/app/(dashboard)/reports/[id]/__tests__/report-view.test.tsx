import { render, waitFor } from "@testing-library/react";
import { beforeEach, it, expect, vi } from "vitest";
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "abc" }), useRouter: () => ({ push: vi.fn(), back: vi.fn() }) }));
const getHtml = vi.hoisted(() => vi.fn().mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>"));
vi.mock("@/lib/api-client", () => ({ apiClient: { getText: getHtml } }));
import ReportViewPage from "@/app/(dashboard)/reports/[id]/page";

beforeEach(() => {
  // jsdom doesn't implement URL.createObjectURL/revokeObjectURL
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
});

it("fetches report HTML via apiClient and renders an iframe", async () => {
  render(<ReportViewPage />);
  await waitFor(() => expect(getHtml).toHaveBeenCalledWith("/api/v1/reports/abc/view"));
  expect(document.querySelector("iframe")).toBeTruthy();
});
