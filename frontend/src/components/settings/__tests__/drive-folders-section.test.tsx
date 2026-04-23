import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DriveFoldersSection } from "../drive-folders-section";

vi.mock("@/hooks/useDriveFolders", () => ({
  useDriveFolders: () => ({
    data: [
      {
        id: "1",
        tenant_id: "t",
        folder_id: "X",
        folder_name: "Policies",
        is_enabled: true,
        sync_status: "success",
        last_synced_at: "2026-04-22T06:00:00Z",
        last_sync_error: null,
        chunk_count: 42,
        file_count: 7,
        created_at: "2026-04-22T00:00:00Z",
      },
    ],
    isLoading: false,
  }),
  useAddDriveFolder: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRemoveDriveFolder: () => ({ mutateAsync: vi.fn() }),
  useToggleDriveFolder: () => ({ mutateAsync: vi.fn() }),
  useSyncDriveFolder: () => ({ mutateAsync: vi.fn() }),
}));

function wrap(children: React.ReactNode) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe("DriveFoldersSection", () => {
  it("lists registered folders with chunk count and file count", () => {
    render(wrap(<DriveFoldersSection />));
    expect(screen.getByText("Policies")).toBeInTheDocument();
    expect(screen.getByText(/42 chunks/)).toBeInTheDocument();
    expect(screen.getByText(/7 files/)).toBeInTheDocument();
  });

  it("renders an Add-folder input with placeholder and Add button", () => {
    render(wrap(<DriveFoldersSection />));
    expect(screen.getByPlaceholderText(/folder URL or ID/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^add$/i })).toBeInTheDocument();
  });

  it("renders Re-Sync and Remove buttons per folder row", () => {
    render(wrap(<DriveFoldersSection />));
    expect(screen.getByRole("button", { name: /re-sync/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /remove/i })).toBeInTheDocument();
  });

  it("shows a human-readable sync status", () => {
    render(wrap(<DriveFoldersSection />));
    // Should show some form of "synced" + relative time or timestamp
    // The current test-data timestamp is 2026-04-22T06:00 — real-time expression depends on now().
    // Just verify SOME indication of sync state is visible.
    const textNodes = screen.getAllByText(/synced|hours|ago/i);
    expect(textNodes.length).toBeGreaterThan(0);
  });
});
