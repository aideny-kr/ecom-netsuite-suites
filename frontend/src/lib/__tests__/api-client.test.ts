import { describe, it, expect, vi, afterEach } from "vitest";
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
