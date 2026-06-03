import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

// Hoisted mock state for the inlined BUILD_ID.
const buildId = vi.hoisted(() => ({ value: "build-aaa" }));
vi.mock("@/lib/build-id", () => ({
  get BUILD_ID() {
    return buildId.value;
  },
}));

import { useVersionCheck } from "@/hooks/use-version-check";

function mockFetchReturning(id: string) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ buildId: id }),
  } as Response);
}

beforeEach(() => {
  buildId.value = "build-aaa";
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useVersionCheck", () => {
  it("sets updateAvailable=true when the server build id differs", async () => {
    vi.stubGlobal("fetch", mockFetchReturning("build-bbb"));
    const { result } = renderHook(() => useVersionCheck());
    await waitFor(() => expect(result.current.updateAvailable).toBe(true));
  });

  it("keeps updateAvailable=false when ids match", async () => {
    const fetchMock = mockFetchReturning("build-aaa");
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useVersionCheck());
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(result.current.updateAvailable).toBe(false);
  });

  it("is a no-op when BUILD_ID is 'dev' (never fetches, never flags)", async () => {
    buildId.value = "dev";
    const fetchMock = mockFetchReturning("build-bbb");
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useVersionCheck());
    // give any effect a tick
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.updateAvailable).toBe(false);
  });

  it("stays false (never flips true) when the fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useVersionCheck());
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(result.current.updateAvailable).toBe(false);
  });

  it("re-checks when the tab becomes visible again", async () => {
    const fetchMock = mockFetchReturning("build-aaa");
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => useVersionCheck());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("re-checks when the window regains focus", async () => {
    const fetchMock = mockFetchReturning("build-aaa");
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => useVersionCheck());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("coalesces concurrent triggers (alt-tab fires focus + visibilitychange) to a single in-flight fetch", async () => {
    // A fetch that stays pending so a second trigger lands while the first is
    // still in flight. Both the 'focus' and 'visibilitychange' from one alt-tab
    // must share a single request.
    let resolveFetch: (value: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal("fetch", fetchMock);

    renderHook(() => useVersionCheck());
    // Mount kicks off the first (still-pending) check.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    act(() => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // Both extra triggers are coalesced while the first request is in flight.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Once it resolves, a subsequent trigger fetches again.
    await act(async () => {
      resolveFetch({
        ok: true,
        json: async () => ({ buildId: "build-aaa" }),
      } as Response);
      await pending;
    });
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});
