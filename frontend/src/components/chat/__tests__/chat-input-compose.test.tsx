import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const get = vi.fn();
vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
  },
}));

import { ChatInput } from "@/components/chat/chat-input";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue([]);
});

describe("ChatInput initialMessage (compose prefill)", () => {
  it("seeds the composer value from initialMessage WITHOUT calling onSend", async () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isLoading={false} initialMessage="/flux " />, {
      wrapper,
    });

    const textarea = screen.getByRole("textbox");
    await waitFor(() => expect(textarea).toHaveValue("/flux "));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("leaves the composer empty when initialMessage is null", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isLoading={false} initialMessage={null} />, {
      wrapper,
    });

    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not clobber user typing on re-render (seeds only once)", async () => {
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatInput onSend={onSend} isLoading={false} initialMessage="/flux " />,
      { wrapper },
    );

    const textarea = screen.getByRole("textbox");
    await waitFor(() => expect(textarea).toHaveValue("/flux "));

    // User edits the seeded value
    fireEvent.change(textarea, { target: { value: "/flux Q2 vs Q1" } });
    expect(textarea).toHaveValue("/flux Q2 vs Q1");

    // A re-render with the same initialMessage must NOT reset the user's edit
    rerender(
      <ChatInput onSend={onSend} isLoading={false} initialMessage="/flux " />,
    );
    expect(textarea).toHaveValue("/flux Q2 vs Q1");
    expect(onSend).not.toHaveBeenCalled();
  });
});
