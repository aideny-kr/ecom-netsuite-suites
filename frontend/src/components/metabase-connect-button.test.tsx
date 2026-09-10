import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import { MetabaseConnectButton } from "./metabase-connect-button";

const mocks = vi.hoisted(() => ({ post: vi.fn(), invalidate: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: { post: mocks.post } }));
vi.mock("@tanstack/react-query", () => ({ useQueryClient: () => ({ invalidateQueries: mocks.invalidate }) }));
let popup: Window;
beforeEach(() => {
  vi.clearAllMocks();
  mocks.post.mockReset();
  popup = { closed: false, close: vi.fn(), location: { href: "" } } as unknown as Window;
  vi.spyOn(window, "open").mockReturnValue(popup);
  mocks.post.mockResolvedValueOnce({ state: "state-1", authorize_url: "https://analytics.example/oauth/authorize" });
});
afterEach(() => { vi.restoreAllMocks(); });
function message(overrides: Partial<MessageEventInit> = {}) {
  window.dispatchEvent(new MessageEvent("message", {
    origin: "http://localhost:8000", source: popup,
    data: { type: "METABASE_OAUTH_RESULT", state: "state-1", code: "code-1" }, ...overrides,
  }));
}
it("opens provider sign-in and completes only a matching popup/origin/state", async () => {
  mocks.post.mockResolvedValueOnce({ status: "ok", message: "Connected" });
  render(<MetabaseConnectButton connectorId="m1" />);
  fireEvent.click(screen.getByRole("button", { name: "Connect with Metabase" }));
  await waitFor(() => expect(popup.location.href).toContain("/oauth/authorize"));
  expect(mocks.post).toHaveBeenCalledWith("/api/v1/mcp-connectors/m1/metabase/authorize", { app_origin: window.location.origin });
  act(() => {
    message({ origin: "https://evil.example" });
    message({ source: window });
    message({ data: { type: "METABASE_OAUTH_RESULT", state: "wrong", code: "code-1" } });
  });
  expect(mocks.post).toHaveBeenCalledTimes(1);
  act(() => { message(); message(); });
  await waitFor(() => expect(mocks.invalidate).toHaveBeenCalled());
  expect(mocks.post).toHaveBeenCalledTimes(2);
  expect(mocks.post).toHaveBeenLastCalledWith("/api/v1/mcp-connectors/m1/metabase/complete", { state: "state-1", code: "code-1", error: undefined });
});
it("makes blocked popups retryable without starting an OAuth request", () => {
  vi.mocked(window.open).mockReturnValue(null);
  render(<MetabaseConnectButton connectorId="m1" />);
  fireEvent.click(screen.getByRole("button", { name: "Connect with Metabase" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Allow popups");
  expect(mocks.post).not.toHaveBeenCalled();
});
it("keeps a verification failure visible and refreshes the card", async () => {
  mocks.post.mockResolvedValueOnce({ status: "error", message: "Sign-in succeeded, but tool discovery failed. Use Test to retry." });
  render(<MetabaseConnectButton connectorId="m1" />);
  fireEvent.click(screen.getByRole("button", { name: "Connect with Metabase" }));
  await waitFor(() => expect(popup.location.href).toContain("/oauth/authorize"));
  act(() => message());
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Use Test to retry"));
  expect(mocks.invalidate).toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Connect with Metabase" })).toBeEnabled();
});
it("does not complete a popup after its tenant/component was unmounted", async () => {
  const view = render(<MetabaseConnectButton connectorId="m1" />);
  fireEvent.click(screen.getByRole("button", { name: "Connect with Metabase" }));
  await waitFor(() => expect(popup.location.href).toContain("/oauth/authorize"));
  view.unmount();
  act(() => message());
  expect(mocks.post).toHaveBeenCalledTimes(1);
  expect(popup.close).toHaveBeenCalled();
});
it("shows canceled consent and permits retry", async () => {
  mocks.post.mockRejectedValueOnce(new Error("Metabase sign-in was canceled or denied. You can try again."));
  render(<MetabaseConnectButton connectorId="m1" />);
  fireEvent.click(screen.getByRole("button", { name: "Connect with Metabase" }));
  await waitFor(() => expect(popup.location.href).toContain("/oauth/authorize"));
  act(() => message({ data: { type: "METABASE_OAUTH_RESULT", state: "state-1", error: "access_denied" } }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("canceled or denied"));
});
