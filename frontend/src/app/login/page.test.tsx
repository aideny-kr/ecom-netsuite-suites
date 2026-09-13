import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LoginPage from "./page";

vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ login: vi.fn() }) }));
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

afterEach(() => vi.unstubAllEnvs());

describe("local company sign-in", () => {
  it("works without Google and does not offer public signup", () => {
    vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", "");
    vi.stubEnv("NEXT_PUBLIC_SINGLE_COMPANY", "true");
    render(<LoginPage />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("Google sign-in option")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create one" })).not.toBeInTheDocument();
  });

  it("retains configured Google login and signup for hosted installs", () => {
    vi.stubEnv("NEXT_PUBLIC_GOOGLE_CLIENT_ID", "test-client-id");
    vi.stubEnv("NEXT_PUBLIC_SINGLE_COMPANY", "false");
    render(<LoginPage />);
    expect(screen.getByText("Google sign-in option")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create one" })).toBeInTheDocument();
  });
});
