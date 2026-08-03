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

// Same switchTenant call as Consumer, but reports whether the returned promise
// resolved or rejected — Consumer's own button deliberately swallows the
// rejection (matching sidebar.tsx's real, non-catching caller), which is fine
// for the cache-only assertions above but hides the outcome the /me-failure
// test below needs to see.
function ConsumerCapture({ onSettled }: { onSettled: (result: "resolved" | "rejected") => void }) {
  const { switchTenant } = useAuth();
  return (
    <button
      type="button"
      onClick={() =>
        switchTenant("tenant-b")
          .then(() => onSettled("resolved"))
          .catch(() => onSettled("rejected"))
      }
    >
      switch
    </button>
  );
}

function LoginConsumer() {
  const { login } = useAuth();
  return (
    <button
      type="button"
      onClick={() => login({ email: "a@b.com", password: "pw" }).catch(() => {})}
    >
      login
    </button>
  );
}

function LoginConsumerCapture({ onSettled }: { onSettled: (result: "resolved" | "rejected") => void }) {
  const { login } = useAuth();
  return (
    <button
      type="button"
      onClick={() =>
        login({ email: "a@b.com", password: "pw" })
          .then(() => onSettled("resolved"))
          .catch(() => onSettled("rejected"))
      }
    >
      login
    </button>
  );
}

function RegisterConsumer() {
  const { register } = useAuth();
  return (
    <button
      type="button"
      onClick={() =>
        register({
          tenant_name: "Acme",
          tenant_slug: "acme",
          email: "a@b.com",
          password: "pw",
          full_name: "A B",
        }).catch(() => {})
      }
    >
      register
    </button>
  );
}

function RegisterConsumerCapture({ onSettled }: { onSettled: (result: "resolved" | "rejected") => void }) {
  const { register } = useAuth();
  return (
    <button
      type="button"
      onClick={() =>
        register({
          tenant_name: "Acme",
          tenant_slug: "acme",
          email: "a@b.com",
          password: "pw",
          full_name: "A B",
        })
          .then(() => onSettled("resolved"))
          .catch(() => onSettled("rejected"))
      }
    >
      register
    </button>
  );
}

// logout's context type is `() => void` (fire-and-forget from the caller's
// perspective, matching sidebar/topbar callers), so unlike login/register/
// switchTenant this consumer can't chain .then/.catch on the call itself —
// tests instead assert on cache/router state via waitFor.
function LogoutConsumer() {
  const { logout } = useAuth();
  return (
    <button type="button" onClick={() => logout()}>
      logout
    </button>
  );
}

function renderWithClient(qc: QueryClient, child: React.ReactNode = <Consumer />) {
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>{child}</AuthProvider>
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

it("clears the cache and rejects the caller when /me fails right after tokens are already installed for the new tenant", async () => {
  // Regression: switch-tenant itself succeeds (tenant B's tokens get
  // installed by setTokens), but the follow-up GET /auth/me throws (network
  // blip, transient 401). Without a guard, setUser/queryClient.clear() would
  // never run — the UI would keep showing tenant A's user and cached data
  // while every subsequent request now authenticates as tenant B.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.get.mockRejectedValueOnce(new Error("me failed"));

  let settled: "resolved" | "rejected" | null = null;
  render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <ConsumerCapture onSettled={(r) => { settled = r; }} />
      </AuthProvider>
    </QueryClientProvider>
  );

  await act(async () => {
    screen.getByRole("button", { name: "switch" }).click();
  });

  // The tenant-B switch-tenant call did succeed, so tokens were installed —
  // the failure only happened on the follow-up /me call. The caller must
  // still see it as a rejection (not silently swallowed).
  await waitFor(() => expect(settled).toBe("rejected"));
  // No tenant-A data may survive rendering under tenant-B's now-installed
  // credentials.
  expect(qc.getQueryData(["dashboard"])).toBeUndefined();
});

it("cancels in-flight queries before clearing the cache on a tenant switch", async () => {
  // `queryClient.clear()` alone does not abort an outstanding fetch —
  // `apiClient` never forwards an AbortSignal — so a stale tenant-A request
  // still in flight when `clear()` runs is a risk of repopulating an
  // unscoped query key now serving tenant B. `cancelQueries()` must run
  // first, in that order.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const cancelSpy = vi.spyOn(qc, "cancelQueries");
  const clearSpy = vi.spyOn(qc, "clear");

  renderWithClient(qc);
  await act(async () => {
    screen.getByRole("button", { name: "switch" }).click();
  });

  await waitFor(() => expect(clearSpy).toHaveBeenCalled());
  expect(cancelSpy).toHaveBeenCalled();
  const cancelOrder = cancelSpy.mock.invocationCallOrder[0];
  const clearOrder = clearSpy.mock.invocationCallOrder[0];
  expect(cancelOrder).toBeLessThan(clearOrder);
});

// The cross-tenant cache leak isn't specific to switchTenant: login, register,
// and logout all run the same setTokens() -> /auth/me -> router.push sequence
// (or, for logout, the reverse) against a QueryClient that survives client-side
// navigation for the life of the tab. The following tests mirror the
// switchTenant coverage above for each of those transitions.

it("clears the entire React Query cache after a login succeeds", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });

  renderWithClient(qc, <LoginConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "login" }).click();
  });

  await waitFor(() =>
    expect(api.post).toHaveBeenCalledWith("/api/v1/auth/login", { email: "a@b.com", password: "pw" })
  );
  await waitFor(() => expect(qc.getQueryData(["dashboard"])).toBeUndefined());
  expect(routerPush).toHaveBeenCalledWith("/dashboard");
});

it("does not clear the cache when the login request fails", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.post.mockRejectedValueOnce(new Error("network error"));

  renderWithClient(qc, <LoginConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "login" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalled());
  // No tokens were ever installed, so there's no new identity to protect — the
  // still-signed-out cache (if any) must survive.
  expect(qc.getQueryData(["dashboard"])).toBeDefined();
});

it("clears the cache and rejects the caller when /me fails right after tokens are already installed for a new login", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.get.mockRejectedValueOnce(new Error("me failed"));

  let settled: "resolved" | "rejected" | null = null;
  renderWithClient(qc, <LoginConsumerCapture onSettled={(r) => { settled = r; }} />);

  await act(async () => {
    screen.getByRole("button", { name: "login" }).click();
  });

  // The login POST did succeed, so tokens were installed — the failure only
  // happened on the follow-up /me call. The caller must still see it as a
  // rejection, and no stale data may survive under the new credentials.
  await waitFor(() => expect(settled).toBe("rejected"));
  expect(qc.getQueryData(["dashboard"])).toBeUndefined();
});

it("cancels in-flight queries before clearing the cache on login", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const cancelSpy = vi.spyOn(qc, "cancelQueries");
  const clearSpy = vi.spyOn(qc, "clear");

  renderWithClient(qc, <LoginConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "login" }).click();
  });

  await waitFor(() => expect(clearSpy).toHaveBeenCalled());
  expect(cancelSpy).toHaveBeenCalled();
  const cancelOrder = cancelSpy.mock.invocationCallOrder[0];
  const clearOrder = clearSpy.mock.invocationCallOrder[0];
  expect(cancelOrder).toBeLessThan(clearOrder);
});

it("clears the entire React Query cache after a register succeeds", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });

  renderWithClient(qc, <RegisterConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "register" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/register", expect.any(Object)));
  await waitFor(() => expect(qc.getQueryData(["dashboard"])).toBeUndefined());
  expect(routerPush).toHaveBeenCalledWith("/dashboard");
});

it("does not clear the cache when the register request fails", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.post.mockRejectedValueOnce(new Error("network error"));

  renderWithClient(qc, <RegisterConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "register" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalled());
  expect(qc.getQueryData(["dashboard"])).toBeDefined();
});

it("clears the cache and rejects the caller when /me fails right after tokens are already installed for a new registration", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.get.mockRejectedValueOnce(new Error("me failed"));

  let settled: "resolved" | "rejected" | null = null;
  renderWithClient(qc, <RegisterConsumerCapture onSettled={(r) => { settled = r; }} />);

  await act(async () => {
    screen.getByRole("button", { name: "register" }).click();
  });

  await waitFor(() => expect(settled).toBe("rejected"));
  expect(qc.getQueryData(["dashboard"])).toBeUndefined();
});

it("cancels in-flight queries before clearing the cache on register", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const cancelSpy = vi.spyOn(qc, "cancelQueries");
  const clearSpy = vi.spyOn(qc, "clear");

  renderWithClient(qc, <RegisterConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "register" }).click();
  });

  await waitFor(() => expect(clearSpy).toHaveBeenCalled());
  expect(cancelSpy).toHaveBeenCalled();
  const cancelOrder = cancelSpy.mock.invocationCallOrder[0];
  const clearOrder = clearSpy.mock.invocationCallOrder[0];
  expect(cancelOrder).toBeLessThan(clearOrder);
});

it("clears the entire React Query cache after logout", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });

  renderWithClient(qc, <LogoutConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "logout" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/logout"));
  await waitFor(() => expect(qc.getQueryData(["dashboard"])).toBeUndefined());
  expect(routerPush).toHaveBeenCalledWith("/login");
});

it("still clears the cache when the logout request itself fails", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["dashboard"], { published: [], active: null, active_is_fallback: false });
  api.post.mockRejectedValueOnce(new Error("network error"));

  renderWithClient(qc, <LogoutConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "logout" }).click();
  });

  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/logout"));
  // logout is best-effort about the server call (see the existing catch around
  // it) — but clearing local state, including the query cache, must not be
  // skipped just because the network call failed. Leaving a signed-out user's
  // data cached is the same leak as the other transitions, with a worse story.
  await waitFor(() => expect(qc.getQueryData(["dashboard"])).toBeUndefined());
  expect(routerPush).toHaveBeenCalledWith("/login");
});

it("cancels in-flight queries before clearing the cache on logout", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const cancelSpy = vi.spyOn(qc, "cancelQueries");
  const clearSpy = vi.spyOn(qc, "clear");

  renderWithClient(qc, <LogoutConsumer />);
  await act(async () => {
    screen.getByRole("button", { name: "logout" }).click();
  });

  await waitFor(() => expect(clearSpy).toHaveBeenCalled());
  expect(cancelSpy).toHaveBeenCalled();
  const cancelOrder = cancelSpy.mock.invocationCallOrder[0];
  const clearOrder = clearSpy.mock.invocationCallOrder[0];
  expect(cancelOrder).toBeLessThan(clearOrder);
});
