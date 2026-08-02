import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { AuthProvider, useAuth } from "@/providers/auth-provider";
import type { AuthResponse, User } from "@/lib/types";

const profile: User = {
  id: "u-1",
  tenant_id: "tenant-b",
  tenant_name: "Tenant B",
  email: "a@b.com",
  full_name: "A B",
  is_active: true,
  onboarding_completed_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const tokens: AuthResponse = {
  access_token: "new-token",
  token_type: "bearer",
};

// Renders AuthProvider (which needs a QueryClientProvider ancestor, same nesting as
// app/layout.tsx) plus a plain consumer that exposes switchTenant via a button, and a
// probe that reads the SAME queryClient instance the test seeds/inspects directly.
function Consumer() {
  const { switchTenant } = useAuth();
  return (
    // Mirrors sidebar.tsx's real caller, which also doesn't catch — swallowed here only
    // so a deliberately-rejected mock in the failure-path test doesn't surface as an
    // unhandled rejection; it isn't part of what these tests assert on.
    <button type="button" onClick={() => switchTenant("tenant-b").catch(() => {})}>
      switch
    </button>
  );
}

function renderWithClient(qc: QueryClient) {
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <Consumer />
      </AuthProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  document.cookie = "access_token=; path=/; max-age=0";
  api.post.mockResolvedValue(tokens);
  api.get.mockResolvedValue(profile);
});

it("clears the entire React Query cache after a tenant switch succeeds", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Seed a cache entry the way useDashboard() would for the PRE-switch tenant (Tenant A).
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  expect(qc.getQueryData(["dashboard"])).toBeDefined();

  renderWithClient(qc);
  await act(async () => {
    screen.getByRole("button", { name: "switch" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/switch-tenant", { tenant_id: "tenant-b" }));
  // The whole cache must be dropped, not just a re-fetch of the same stale key — any
  // other query the app happens to have cached for Tenant A must go too.
  await waitFor(() => expect(qc.getQueryData(["dashboard"])).toBeUndefined());
  expect(routerPush).toHaveBeenCalledWith("/dashboard");
});

it("does not clear the cache when the switch-tenant request fails", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.post.mockRejectedValueOnce(new Error("network error"));

  renderWithClient(qc);
  await act(async () => {
    screen.getByRole("button", { name: "switch" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalled());
  // A failed switch must not wipe the current (still-valid) tenant's cache.
  expect(qc.getQueryData(["dashboard"])).toBeDefined();
});
