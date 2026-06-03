import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render } from "@testing-library/react";

const reloadOnceForStaleChunk = vi.fn();
const isStaleChunkError = vi.fn();
vi.mock("@/lib/recover-from-stale-chunk", () => ({
  reloadOnceForStaleChunk: () => reloadOnceForStaleChunk(),
  isStaleChunkError: (err: unknown) => isStaleChunkError(err),
}));

import { ChunkReloadGuard } from "@/components/chunk-reload-guard";

beforeEach(() => {
  reloadOnceForStaleChunk.mockReset();
  isStaleChunkError.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ChunkReloadGuard", () => {
  it("triggers a reload on a window 'error' event that is a stale chunk", () => {
    isStaleChunkError.mockReturnValue(true);
    render(<ChunkReloadGuard />);

    const evt = new ErrorEvent("error", {
      error: Object.assign(new Error("Loading chunk 5 failed."), {
        name: "ChunkLoadError",
      }),
    });
    window.dispatchEvent(evt);

    expect(reloadOnceForStaleChunk).toHaveBeenCalledTimes(1);
  });

  it("triggers a reload on an 'unhandledrejection' for a stale chunk", () => {
    isStaleChunkError.mockReturnValue(true);
    render(<ChunkReloadGuard />);

    const reason = new Error("Failed to fetch dynamically imported module");
    // jsdom does not construct PromiseRejectionEvent reliably; use a plain Event.
    const evt = new Event("unhandledrejection") as Event & { reason?: unknown };
    evt.reason = reason;
    window.dispatchEvent(evt);

    expect(reloadOnceForStaleChunk).toHaveBeenCalledTimes(1);
  });

  it("does nothing for a non-chunk error", () => {
    isStaleChunkError.mockReturnValue(false);
    render(<ChunkReloadGuard />);

    window.dispatchEvent(
      new ErrorEvent("error", { error: new Error("some other bug") }),
    );

    expect(reloadOnceForStaleChunk).not.toHaveBeenCalled();
  });

  it("removes its listeners on unmount (no reload after unmount)", () => {
    isStaleChunkError.mockReturnValue(true);
    const { unmount } = render(<ChunkReloadGuard />);
    unmount();

    // Swallow the synthetic error so jsdom does not report it as unhandled
    // (the guard is unmounted, so nothing else consumes it).
    const swallow = (e: ErrorEvent) => e.preventDefault();
    window.addEventListener("error", swallow);
    window.dispatchEvent(
      new ErrorEvent("error", {
        error: new Error("Loading chunk 1 failed."),
        cancelable: true,
      }),
    );
    window.removeEventListener("error", swallow);

    expect(reloadOnceForStaleChunk).not.toHaveBeenCalled();
  });
});
