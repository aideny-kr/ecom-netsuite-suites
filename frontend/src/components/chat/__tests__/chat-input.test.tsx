import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/lib/api-client";
import { ChatInput } from "../chat-input";

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: vi.fn(),
  },
}));

function renderWithQueryClient(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

describe("ChatInput attachments", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
      if (path === "/api/v1/chat/health") return { max_input_chars: 32000 };
      return [];
    });
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

describe("ChatInput length limit", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
      if (path === "/api/v1/chat/health") return { max_input_chars: 32000 };
      return [];
    });
  });

  it("does not silently truncate messages above the old 4000 character cap", () => {
    const onSend = vi.fn();
    renderWithQueryClient(<ChatInput onSend={onSend} isLoading={false} />);

    const input = screen.getByPlaceholderText(/ask a question/i);
    fireEvent.change(input, { target: { value: "x".repeat(4001) } });

    expect(input).toHaveValue("x".repeat(4001));
    expect(screen.getByText("4,001/32,000")).toBeInTheDocument();
  });

  it("warns near the configured limit and blocks sends above it", () => {
    const onSend = vi.fn();
    renderWithQueryClient(<ChatInput onSend={onSend} isLoading={false} />);

    const input = screen.getByPlaceholderText(/ask a question/i);
    fireEvent.change(input, { target: { value: "x".repeat(28800) } });
    expect(screen.getByText("3,200 chars left")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "x".repeat(32001) } });
    expect(screen.getByText("Reduce by 1 char")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /send message/i })).toBeDisabled();
  });
});
