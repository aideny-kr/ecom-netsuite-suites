import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const reloadOnceForStaleChunk = vi.fn();
const isStaleChunkError = vi.fn();
vi.mock("@/lib/recover-from-stale-chunk", () => ({
  reloadOnceForStaleChunk: () => reloadOnceForStaleChunk(),
  isStaleChunkError: (err: unknown) => isStaleChunkError(err),
}));

import ErrorBoundary from "@/app/error";
import GlobalError from "@/app/global-error";

const reload = vi.fn();

beforeEach(() => {
  reloadOnceForStaleChunk.mockReset();
  isStaleChunkError.mockReset();
  reload.mockReset();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("app/error.tsx (segment boundary)", () => {
  it("auto-recovers on a stale chunk error when a reload actually fires (no fallback shown)", () => {
    isStaleChunkError.mockReturnValue(true);
    reloadOnceForStaleChunk.mockReturnValue(true);
    render(
      <ErrorBoundary
        error={Object.assign(new Error("Loading chunk 9 failed."), {
          name: "ChunkLoadError",
        })}
        reset={vi.fn()}
      />,
    );
    expect(reloadOnceForStaleChunk).toHaveBeenCalledTimes(1);
    // A reload is navigating away — no visible fallback button.
    expect(screen.queryByRole("button", { name: /reload/i })).toBeNull();
  });

  it("shows the branded fallback with a Reload button when a stale-chunk reload is GUARDED (returns false)", () => {
    isStaleChunkError.mockReturnValue(true);
    reloadOnceForStaleChunk.mockReturnValue(false);
    render(
      <ErrorBoundary
        error={Object.assign(new Error("Loading chunk 9 failed."), {
          name: "ChunkLoadError",
        })}
        reset={vi.fn()}
      />,
    );
    expect(reloadOnceForStaleChunk).toHaveBeenCalledTimes(1);
    // No dead-end blank page: the fallback is visible and Reload hard-reloads.
    const button = screen.getByRole("button", { name: /reload/i });
    fireEvent.click(button);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("shows a fallback + Reload button for a non-chunk error and calls reset", () => {
    isStaleChunkError.mockReturnValue(false);
    const reset = vi.fn();
    render(<ErrorBoundary error={new Error("boom")} reset={reset} />);
    expect(reloadOnceForStaleChunk).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /reload/i }));
    expect(reset).toHaveBeenCalledTimes(1);
  });
});

describe("app/global-error.tsx (root boundary)", () => {
  it("auto-recovers on a stale chunk error when a reload actually fires (no fallback shown)", () => {
    isStaleChunkError.mockReturnValue(true);
    reloadOnceForStaleChunk.mockReturnValue(true);
    render(
      <GlobalError
        error={Object.assign(new Error("Importing a module script failed."), {
          name: "ChunkLoadError",
        })}
        reset={vi.fn()}
      />,
    );
    expect(reloadOnceForStaleChunk).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /reload/i })).toBeNull();
  });

  it("shows the branded fallback with a Reload button when a stale-chunk reload is GUARDED (returns false)", () => {
    isStaleChunkError.mockReturnValue(true);
    reloadOnceForStaleChunk.mockReturnValue(false);
    render(
      <GlobalError
        error={Object.assign(new Error("Importing a module script failed."), {
          name: "ChunkLoadError",
        })}
        reset={vi.fn()}
      />,
    );
    expect(reloadOnceForStaleChunk).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /reload/i }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("renders a fallback with a Reload button for a non-chunk error", () => {
    isStaleChunkError.mockReturnValue(false);
    render(<GlobalError error={new Error("fatal")} reset={vi.fn()} />);
    expect(reloadOnceForStaleChunk).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /reload/i }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
