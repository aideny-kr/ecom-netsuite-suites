import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatInput } from "../chat-input";

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: vi.fn().mockResolvedValue([]),
  },
}));

function renderWithQueryClient(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

describe("ChatInput attachments", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("clears the previous attachment when replacement file validation fails", async () => {
    const onSend = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ id: "file-old", filename: "old.xlsx" }),
      }),
    );

    const { container } = renderWithQueryClient(<ChatInput onSend={onSend} isLoading={false} />);
    const fileInput = container.querySelector("input[type='file']") as HTMLInputElement;

    fireEvent.change(fileInput, {
      target: { files: [new File(["old"], "old.xlsx")] },
    });
    expect(await screen.findByText("old.xlsx")).toBeInTheDocument();

    fireEvent.change(fileInput, {
      target: { files: [new File(["bad"], "bad.exe")] },
    });
    expect(screen.queryByText("old.xlsx")).not.toBeInTheDocument();
    expect(screen.getByText("Attach an Excel, CSV, or JSON file.")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/ask a question/i), {
      target: { value: "Analyze this" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send message/i }));

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("Analyze this", undefined));
  });
});
