import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import InviteAcceptPage from "./page";

vi.mock("next/navigation", () => ({
  useParams: () => ({ token: "test-invite" }),
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn().mockResolvedValue({
    email: "invite@example.com", role_name: "admin", role_display_name: "Admin",
    tenant_name: "Test Company", status: "pending", expired: false,
  }) },
}));
// Use the real Google component: mounting it without a provider caused the crash.
afterEach(() => vi.unstubAllEnvs());
it("lets an invitee choose a password when Google OAuth is unconfigured", async () => {
  vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", "");
  render(<InviteAcceptPage />);
  expect(await screen.findByRole("button", { name: "Create Account" })).toBeEnabled();
  expect(screen.getByLabelText("Password")).toBeInTheDocument();
  expect(screen.getByText("Admin")).toBeInTheDocument();
  expect(screen.getByText("invite@example.com")).toBeInTheDocument();
});
