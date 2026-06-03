import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Hoisted mock state for the inlined BUILD_ID so we can exercise the dev gate.
const buildId = vi.hoisted(() => ({ value: "build-aaa" }));
vi.mock("@/lib/build-id", () => ({
  get BUILD_ID() {
    return buildId.value;
  },
}));

import {
  isStaleChunkError,
  reloadOnceForStaleChunk,
} from "@/lib/recover-from-stale-chunk";

describe("isStaleChunkError", () => {
  it("matches an error whose name is ChunkLoadError", () => {
    const err = new Error("anything");
    err.name = "ChunkLoadError";
    expect(isStaleChunkError(err)).toBe(true);
  });

  it("matches 'Loading chunk N failed' messages", () => {
    expect(
      isStaleChunkError(new Error("Loading chunk 4823 failed.")),
    ).toBe(true);
  });

  it("matches 'Failed to fetch dynamically imported module'", () => {
    expect(
      isStaleChunkError(
        new Error(
          "Failed to fetch dynamically imported module: https://x/_next/static/chunks/abc.js",
        ),
      ),
    ).toBe(true);
  });

  it("matches 'Importing a module script failed'", () => {
    expect(
      isStaleChunkError(new Error("Importing a module script failed.")),
    ).toBe(true);
  });

  it("rejects an ordinary error", () => {
    expect(isStaleChunkError(new Error("Cannot read properties of undefined"))).toBe(
      false,
    );
  });

  it("rejects non-error values", () => {
    expect(isStaleChunkError(null)).toBe(false);
    expect(isStaleChunkError(undefined)).toBe(false);
    expect(isStaleChunkError("Loading chunk 1 failed")).toBe(false);
  });
});

describe("reloadOnceForStaleChunk", () => {
  const reload = vi.fn();
  let store: Record<string, string>;

  beforeEach(() => {
    buildId.value = "build-aaa";
    reload.mockReset();
    store = {};
    vi.stubGlobal("sessionStorage", {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = String(v);
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    } as unknown as Storage);
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("reloads once, records the timestamp, and returns true", () => {
    expect(reloadOnceForStaleChunk()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(store["__sb_chunk_reload_at"]).toBeTruthy();
  });

  it("does NOT reload a second time within the 10s guard window and returns false", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-03T00:00:00Z"));
    expect(reloadOnceForStaleChunk()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);

    // 5 seconds later — still inside the guard window
    vi.setSystemTime(new Date("2026-06-03T00:00:05Z"));
    expect(reloadOnceForStaleChunk()).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("reloads again (returns true) after the 10s guard window elapses", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-03T00:00:00Z"));
    expect(reloadOnceForStaleChunk()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);

    // 11 seconds later — guard window expired
    vi.setSystemTime(new Date("2026-06-03T00:00:11Z"));
    expect(reloadOnceForStaleChunk()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("returns false and does NOT reload when BUILD_ID is 'dev' (HMR dev guard)", () => {
    buildId.value = "dev";
    expect(reloadOnceForStaleChunk()).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    // It must not even claim the guard slot in dev.
    expect(store["__sb_chunk_reload_at"]).toBeUndefined();
  });

  it("returns false (no reload) when sessionStorage is unavailable", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {},
    } as unknown as Storage);
    expect(reloadOnceForStaleChunk()).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});
