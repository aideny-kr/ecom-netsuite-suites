import { fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "abc" }),
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));
const api = vi.hoisted(() => ({
  getText: vi.fn(),
  get: vi.fn(),
  post: vi.fn(),
}));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import ReportViewPage from "@/app/(dashboard)/reports/[id]/page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return render(<ReportViewPage />, { wrapper: Wrapper });
}

const _report = (over: object = {}) => ({
  id: "abc",
  title: "Live",
  status: "draft",
  version: 2,
  created_at: "2026-07-06T18:00:00Z",
  has_recipe: true,
  last_refreshed_at: "2026-07-07T03:00:00Z",
  ...over,
});

const _versions = [
  { version: 2, created_at: "2026-07-07T03:00:00Z", pinned: false, is_current: true },
  { version: 1, created_at: "2026-07-06T18:00:00Z", pinned: false, is_current: false },
];

beforeEach(() => {
  vi.clearAllMocks();
  // jsdom doesn't implement URL.createObjectURL/revokeObjectURL
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
  api.getText.mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>");
  api.get.mockImplementation((path: string) =>
    path.endsWith("/versions") ? Promise.resolve(_versions) : Promise.resolve(_report())
  );
});

it("fetches report HTML via apiClient and renders an iframe", async () => {
  renderPage();
  await waitFor(() => expect(api.getText).toHaveBeenCalledWith("/api/v1/reports/abc/view"));
  expect(document.querySelector("iframe")).toBeTruthy();
});

it("renders the report iframe fully sandboxed (no scripts/forms/popups/same-origin)", async () => {
  // Gate D (finding #11): a blob: iframe inherits the embedding page's origin, so any
  // HTML/SVG injection in rendered_html would execute with full same-origin privileges.
  // The artifact is static HTML+CSS+inline SVG and needs NO scripts/forms/popups, so the
  // iframe must carry an EMPTY sandbox attribute (most restrictive: null origin, no script).
  renderPage();
  await waitFor(() => expect(document.querySelector("iframe")).toBeTruthy());
  const iframe = document.querySelector("iframe")!;
  expect(iframe.hasAttribute("sandbox")).toBe(true);
  expect(iframe.getAttribute("sandbox")).toBe("");
});

it("offers a Download HTML action once the report is loaded (save-as-page)", async () => {
  const { findByRole } = renderPage();
  const btn = await findByRole("button", { name: /download html/i });
  expect(btn).toBeTruthy();
});

// --- Slice B: Refresh button, version picker, "data as of" stamp ---------------------

it("shows Refresh only for recipe-bearing reports", async () => {
  api.get.mockImplementation((path: string) =>
    path.endsWith("/versions")
      ? Promise.resolve([_versions[0]])
      : Promise.resolve(_report({ has_recipe: false }))
  );
  const { queryByRole, findByText } = renderPage();
  await findByText(/data as of/i); // metadata loaded
  expect(queryByRole("button", { name: /refresh/i })).toBeNull();
});

it("Refresh posts, then re-fetches the current view HTML", async () => {
  api.post.mockResolvedValue(_report({ version: 3 }));
  const { findByRole } = renderPage();
  const btn = await findByRole("button", { name: /^refresh$/i });
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(1));
  fireEvent.click(btn);
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/reports/abc/refresh"));
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(2)); // the new current HTML
  expect(api.getText).toHaveBeenLastCalledWith("/api/v1/reports/abc/view");
});

it("a refresh error shows the backend message and keeps the last good iframe", async () => {
  api.post.mockRejectedValue(new Error("refreshed recently — try again in about 240s"));
  const { findByRole, findByText } = renderPage();
  const btn = await findByRole("button", { name: /^refresh$/i });
  fireEvent.click(btn);
  await findByText(/refreshed recently/i);
  expect(document.querySelector("iframe")).toBeTruthy(); // last good HTML still shown
});

it("selecting a historical version fetches its snapshot and disables Refresh", async () => {
  const { findByLabelText, findByRole } = renderPage();
  const picker = (await findByLabelText(/report version/i)) as HTMLSelectElement;
  fireEvent.change(picker, { target: { value: "1" } });
  await waitFor(() =>
    expect(api.getText).toHaveBeenLastCalledWith("/api/v1/reports/abc/versions/1/view")
  );
  const btn = await findByRole("button", { name: /^refresh$/i });
  expect((btn as HTMLButtonElement).disabled).toBe(true); // never refresh from a historical view
});

it("the iframe stays fully sandboxed after a refresh re-render", async () => {
  api.post.mockResolvedValue(_report({ version: 3 }));
  const { findByRole } = renderPage();
  const btn = await findByRole("button", { name: /^refresh$/i });
  fireEvent.click(btn);
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(2));
  const iframe = document.querySelector("iframe")!;
  expect(iframe.getAttribute("sandbox")).toBe("");
});

it("shows the data-as-of stamp from last_refreshed_at", async () => {
  const { findByText } = renderPage();
  expect(await findByText(/data as of/i)).toBeTruthy();
});
