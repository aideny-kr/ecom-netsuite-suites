import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { DriveFileMentionPicker } from "../drive-file-mention-picker";
import type { DriveFileListItem } from "@/hooks/useDriveFolders";

// Mock the hook so tests don't hit the network.
vi.mock("@/hooks/useDriveFolders", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/useDriveFolders")>(
    "@/hooks/useDriveFolders",
  );
  return { ...actual, useDriveFiles: vi.fn() };
});

import { useDriveFiles } from "@/hooks/useDriveFolders";

const mockUseDriveFiles = vi.mocked(useDriveFiles);

const FILES: DriveFileListItem[] = [
  {
    id: "u1",
    drive_file_id: "g1",
    name: "Returns Policy",
    mime_type: "application/vnd.google-apps.document",
    web_view_link: "https://docs.google.com/document/d/g1/edit",
    folder_name: "Policies",
    chunk_count: 12,
  },
  {
    id: "u2",
    drive_file_id: "g2",
    name: "Returns Addendum 2026",
    mime_type: "application/vnd.google-apps.document",
    web_view_link: "https://docs.google.com/document/d/g2/edit",
    folder_name: "Policies",
    chunk_count: 3,
  },
];

function wrapper(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  mockUseDriveFiles.mockReset();
  mockUseDriveFiles.mockReturnValue({
    data: FILES,
    isLoading: false,
    isError: false,
  } as ReturnType<typeof useDriveFiles>);
});

describe("DriveFileMentionPicker", () => {
  it("renders nothing when open=false", () => {
    const { container } = render(
      wrapper(
        <DriveFileMentionPicker
          open={false}
          query="ret"
          onSelect={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    expect(container.querySelector("[role='listbox']")).toBeNull();
  });

  it("renders file rows when open", () => {
    render(
      wrapper(
        <DriveFileMentionPicker
          open
          query="ret"
          onSelect={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    expect(screen.getByText("Returns Policy")).toBeInTheDocument();
    expect(screen.getByText("Returns Addendum 2026")).toBeInTheDocument();
  });

  it("calls onSelect with {name, url} when a row is clicked", () => {
    const onSelect = vi.fn();
    render(
      wrapper(
        <DriveFileMentionPicker open query="ret" onSelect={onSelect} onClose={() => {}} />,
      ),
    );
    fireEvent.click(screen.getByText("Returns Policy"));
    expect(onSelect).toHaveBeenCalledWith({
      name: "Returns Policy",
      url: "https://docs.google.com/document/d/g1/edit",
    });
  });

  it("calls onSelect on Enter with the active row", () => {
    const onSelect = vi.fn();
    render(
      wrapper(
        <DriveFileMentionPicker open query="ret" onSelect={onSelect} onClose={() => {}} />,
      ),
    );
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith({
      name: "Returns Policy",
      url: "https://docs.google.com/document/d/g1/edit",
    });
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    render(
      wrapper(
        <DriveFileMentionPicker open query="ret" onSelect={() => {}} onClose={onClose} />,
      ),
    );
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("arrow-down moves active selection to the next row", () => {
    const onSelect = vi.fn();
    render(
      wrapper(
        <DriveFileMentionPicker open query="ret" onSelect={onSelect} onClose={() => {}} />,
      ),
    );
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "ArrowDown" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith({
      name: "Returns Addendum 2026",
      url: "https://docs.google.com/document/d/g2/edit",
    });
  });

  it("arrow-up from top wraps to the last row", () => {
    const onSelect = vi.fn();
    render(
      wrapper(
        <DriveFileMentionPicker open query="ret" onSelect={onSelect} onClose={() => {}} />,
      ),
    );
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "ArrowUp" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith({
      name: "Returns Addendum 2026",
      url: "https://docs.google.com/document/d/g2/edit",
    });
  });

  it("shows an empty state when no files match", () => {
    mockUseDriveFiles.mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof useDriveFiles>);
    render(
      wrapper(
        <DriveFileMentionPicker open query="xyz" onSelect={() => {}} onClose={() => {}} />,
      ),
    );
    expect(screen.getByText(/no drive files/i)).toBeInTheDocument();
  });

  it("shows a loading state while the query is in flight", () => {
    mockUseDriveFiles.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    } as unknown as ReturnType<typeof useDriveFiles>);
    render(
      wrapper(
        <DriveFileMentionPicker open query="ret" onSelect={() => {}} onClose={() => {}} />,
      ),
    );
    expect(screen.getByText(/searching/i)).toBeInTheDocument();
  });
});
