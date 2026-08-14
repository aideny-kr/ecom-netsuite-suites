import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/**
 * Gate round 8, major. The previous fix added an `error` field to useOnboardingChat
 * and stopped deleting the user's optimistic message -- but NEITHER consumer
 * destructured `error`, so the 429 became completely invisible. Removing the
 * message-deletion took away the only (crude) signal a send had failed, and the
 * replacement signal was never rendered.
 *
 * The hook-level test passed throughout, because it asserted the hook's return value
 * rather than what a user can see. These render the real components instead.
 */

const hookState = vi.hoisted(() => ({
  value: {
    messages: [] as unknown[],
    sendMessage: vi.fn(),
    startSession: vi.fn(),
    isLoading: false,
    isStarting: false,
    isComplete: false,
    error: null as string | null,
    sessionId: "s1",
    wizardStep: null as string | null,
    setWizardStep: vi.fn(),
  },
}));
vi.mock("@/hooks/use-onboarding-chat", () => ({
  useOnboardingChat: () => hookState.value,
}));

import { OnboardingOverlay } from "@/components/onboarding/onboarding-overlay";
import { ChatCopilotPanel } from "@/components/onboarding/chat-copilot-panel";

afterEach(() => {
  hookState.value.error = null;
  vi.clearAllMocks();
});

const RATE_LIMIT = "Too many messages. Limit is 20 per minute.";

/** These components sit under the dashboard's react-query provider in the app. */
function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("onboarding surfaces a rate limit to the user", () => {
  it("OnboardingOverlay renders the error", async () => {
    hookState.value.error = RATE_LIMIT;
    renderWithQuery(<OnboardingOverlay onComplete={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toMatch(/too many messages/i);
    });
  });

  it("ChatCopilotPanel renders the error", async () => {
    hookState.value.error = RATE_LIMIT;
    renderWithQuery(<ChatCopilotPanel wizardStep="profile" />);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toMatch(/too many messages/i);
    });
  });

  it("shows nothing when there is no error", () => {
    hookState.value.error = null;
    renderWithQuery(<ChatCopilotPanel wizardStep="profile" />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
