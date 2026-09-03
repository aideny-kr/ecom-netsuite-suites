import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { apiClient, ApiError } from "@/lib/api-client";

// Review-fix round 1, Task 14 finding #1 — `request()`'s thrown Error carried
// no status code at all, so `celigo-flow-page.tsx`'s `is404()` could only
// ever match a real 404 via a message-text regex — and the backend always
// overwrites the message with its own `detail` field (e.g. `{"detail":
// "Flow not found"}`), which never contains the literal string "404". These
// tests exercise the REAL fetch → request() path (not a hand-built fixture)
// to prove a genuine 404 response now carries a real `.status` on the
// thrown error, independent of whatever text the backend's `detail` field
// happens to contain.

describe("apiClient — thrown error carries the real HTTP status", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches status 404 even though the backend's detail text contains no '404' substring", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ detail: "Flow not found" }),
      } as Response),
    );

    await expect(apiClient.get("/api/v1/celigo/flows/unknown-id")).rejects.toMatchObject({
      status: 404,
      message: "Flow not found",
    });
  });

  it("thrown error is an ApiError (still an Error) carrying the response status for any failure, not just 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ detail: "Internal error" }),
      } as Response),
    );

    let caught: unknown;
    try {
      await apiClient.get("/api/v1/whatever");
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toBeInstanceOf(Error);
    expect((caught as ApiError).status).toBe(500);
  });
});

// Codex fix wave, item 17 — after a SUCCESSFUL silent token refresh, the
// retried request used to fall through to the logout path on ANY non-OK
// status. A 404 (the flow id genuinely isn't in the last sync) therefore
// wiped the session and bounced the reader to /login instead of surfacing as
// an ordinary `ApiError` the page could render as "This flow is not in the
// last sync." Only a 401 on the RETRY means the session is really dead.
describe("apiClient — a non-401 failure on the post-refresh retry is not a logout", () => {
  let locationStub: { href: string };

  beforeEach(() => {
    localStorage.setItem("access_token", "stale-token");
    locationStub = { href: "http://localhost/" };
    vi.stubGlobal("location", locationStub);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  /** 401 on the original request → refresh succeeds → the retry answers with
   * `status`. */
  function stubRefreshThen(status: number, detail: string) {
    const fetchMock = vi
      .fn()
      // 1. the original request
      .mockResolvedValueOnce({ ok: false, status: 401, json: async () => ({ detail: "Not authenticated" }) })
      // 2. POST /auth/refresh
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ access_token: "fresh-token" }) })
      // 3. the retry
      .mockResolvedValueOnce({ ok: false, status, json: async () => ({ detail }) });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("401 → refresh ok → 404 throws ApiError(404) and never logs out", async () => {
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");
    stubRefreshThen(404, "Flow not found");

    let caught: unknown;
    try {
      await apiClient.get("/api/v1/celigo/flows/unknown-id");
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(404);
    expect((caught as ApiError).message).toBe("Flow not found");
    expect(locationStub.href).toBe("http://localhost/");
    expect(removeItem).not.toHaveBeenCalledWith("access_token");
    expect(localStorage.getItem("access_token")).toBe("fresh-token");
  });

  it("401 → refresh ok → 500 throws ApiError(500) and never logs out", async () => {
    stubRefreshThen(500, "Internal error");

    await expect(apiClient.get("/api/v1/celigo/flows/any")).rejects.toMatchObject({
      status: 500,
      message: "Internal error",
    });
    expect(locationStub.href).toBe("http://localhost/");
  });

  it("401 → refresh ok → 401 again DOES log out (the session really is dead)", async () => {
    stubRefreshThen(401, "Not authenticated");

    await expect(apiClient.get("/api/v1/celigo/flows/any")).rejects.toThrow("Unauthorized");
    expect(locationStub.href).toBe("/login");
    expect(localStorage.getItem("access_token")).toBeNull();
  });
});
