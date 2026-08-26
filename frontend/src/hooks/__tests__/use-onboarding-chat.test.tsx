import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

const apiPost = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api-client", () => ({
  apiClient: {
    post: apiPost,
  },
}));

import { useOnboardingChat } from "@/hooks/use-onboarding-chat";

/** sendMessage no-ops without a session, so every case has to start one first. */
async function startedSession(result: { current: ReturnType<typeof useOnboardingChat> }) {
  apiPost.mockResolvedValueOnce({
    session_id: "sess-1",
    message: {
      id: "greeting-1",
      role: "assistant",
      content: "Welcome!",
      created_at: new Date().toISOString(),
    },
  });
  await act(async () => {
    await result.current.startSession();
  });
  await waitFor(() => expect(result.current.sessionId).toBe("sess-1"));
}

afterEach(() => {
  vi.restoreAllMocks();
  apiPost.mockReset();
});

/**
 * Gate round 6, major. The backend gained a per-minute chat burst cap that returns
 * 429 from the onboarding endpoint too, and this hook's catch deleted the user's
 * optimistic message and only console.error'd.
 *
 * So a throttled user watched what they typed disappear with no explanation and no
 * way to recover it -- during ONBOARDING, the first thing they ever do in the
 * product. A rate limit is a "wait a moment", not a "your message never existed".
 */
describe("useOnboardingChat rate limiting", () => {
  it("surfaces a 429 as an error instead of silently eating the message", async () => {
    const { result } = renderHook(() => useOnboardingChat());
    await startedSession(result);
    apiPost.mockRejectedValueOnce(new Error("Too many messages. Limit is 20 per minute."));

    await act(async () => {
      await result.current.sendMessage("hello there");
    });

    await waitFor(() => {
      expect(result.current.error).toBeTruthy();
    });
    expect(result.current.error).toMatch(/too many messages/i);
  });

  it("keeps the user's message visible so a throttled turn can be retried", async () => {
    const { result } = renderHook(() => useOnboardingChat());
    await startedSession(result);
    apiPost.mockRejectedValueOnce(new Error("Too many messages. Limit is 20 per minute."));

    await act(async () => {
      await result.current.sendMessage("please do not vanish");
    });

    await waitFor(() => {
      expect(result.current.error).toBeTruthy();
    });
    expect(result.current.messages.some((m) => m.content === "please do not vanish")).toBe(
      true,
    );
  });

  it("clears the loading flag so the composer is usable again", async () => {
    const { result } = renderHook(() => useOnboardingChat());
    await startedSession(result);
    apiPost.mockRejectedValueOnce(new Error("Too many messages. Limit is 20 per minute."));

    await act(async () => {
      await result.current.sendMessage("hi");
    });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
  });
});
