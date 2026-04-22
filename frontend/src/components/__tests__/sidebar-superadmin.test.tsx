import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

vi.mock("@/providers/auth-provider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/providers/branding-provider", () => ({
  useBranding: vi.fn(() => ({ brandName: "Test", logoUrl: null })),
}));

vi.mock("@/hooks/use-features", () => ({
  useFeatures: vi.fn(() => ({ data: {} })),
}));

vi.mock("@/hooks/use-agents", () => ({
  useAgents: vi.fn(() => ({ data: [] })),
}));

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(() => "/dashboard"),
  useRouter: vi.fn(() => ({ push: vi.fn() })),
  useSearchParams: vi.fn(() => ({ get: vi.fn(() => null) })),
}));

vi.mock("next-themes", () => ({
  useTheme: vi.fn(() => ({ theme: "light", setTheme: vi.fn(), resolvedTheme: "light" })),
}));

import { useAuth } from "@/providers/auth-provider";
import { Sidebar } from "../sidebar";

const baseUser = {
  id: "u1",
  tenant_id: "t1",
  tenant_name: "Acme",
  email: "test@example.com",
  full_name: "Test User",
  is_active: true,
  onboarding_completed_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("Sidebar super-admin gating", () => {
  it("hides agent-lab for non-super-admin users", () => {
    (useAuth as ReturnType<typeof vi.fn>).mockReturnValue({
      user: { ...baseUser, global_role: "user" },
      tenants: [],
      logout: vi.fn(),
    });
    render(<Sidebar />);
    expect(screen.queryByText(/agent lab/i)).not.toBeInTheDocument();
  });

  it("shows agent-lab for super-admin users", () => {
    (useAuth as ReturnType<typeof vi.fn>).mockReturnValue({
      user: { ...baseUser, global_role: "superadmin" },
      tenants: [],
      logout: vi.fn(),
    });
    render(<Sidebar />);
    expect(screen.getByText(/agent lab/i)).toBeInTheDocument();
  });
});
