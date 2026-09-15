import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LoginPage from "./page";

const { login } = vi.hoisted(() => ({ login: vi.fn().mockResolvedValue(undefined) }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ login }) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("next/dynamic", () => ({
  default: () => function GoogleLoginStub() {
    if (!process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID) {
      throw new Error("Google login mounted without its provider");
    }
    return <div>Google sign-in option</div>;
  },
}));

beforeEach(() => {
  localStorage.clear();
  login.mockClear();
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("sign-in presentation", () => {
  it("works without Google and preserves the existing signup link", () => {
    vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", "");
    vi.stubEnv("NEXT_PUBLIC_SINGLE_COMPANY", "false");
    render(<LoginPage />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("Google sign-in option")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create one" })).toBeInTheDocument();
  });

  it("retains configured Google login and signup for hosted installs", () => {
    vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", "test-client-id");
    vi.stubEnv("NEXT_PUBLIC_SINGLE_COMPANY", "false");
    render(<LoginPage />);
    expect(screen.getByText("Google sign-in option")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create one" })).toBeInTheDocument();
  });
});

it("keeps password sign-in working when background motion is paused", async () => {
  vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", "");
  render(<LoginPage />);
  fireEvent.click(screen.getByRole("button", { name: "Pause background animation" }));
  expect(screen.getByRole("button", { name: "Resume background animation" })).toHaveAttribute("aria-pressed", "true");
  expect(localStorage.getItem("orbital-motion-paused")).toBe("true");
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "operator@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(login).toHaveBeenCalledWith({ email: "operator@example.com", password: "test-password" }));
});


it.each(["", "test-client-id"])("hides public signup for a dedicated company with Google config %s", (googleClientId) => {
  vi.stubEnv("NEXT_PUBLIC_SINGLE_COMPANY", "true");
  vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", googleClientId);
  render(<LoginPage />);
  expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Create one" })).not.toBeInTheDocument();
});
