import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThinkingBlock, StreamingThinkingBlock, StatusHeadline } from "../message-list";

vi.mock("react-markdown", () => ({ default: ({ children }: any) => <div>{children}</div> }));
vi.mock("react-syntax-highlighter", () => ({ Prism: () => null }));
vi.mock("react-syntax-highlighter/dist/esm/styles/prism", () => ({ oneDark: {} }));
vi.mock("remark-gfm", () => ({ default: () => {} }));
vi.mock("@/hooks/use-saved-queries", () => ({ useCreateSavedQuery: () => ({ mutate: vi.fn() }) }));
vi.mock("@/providers/branding-provider", () => ({ useBranding: () => ({ brandColor: "#000" }) }));
vi.mock("@/hooks/use-chat-feedback", () => ({ useChatFeedback: () => ({ submitFeedback: vi.fn() }) }));
vi.mock("@/hooks/use-agent-instructions", () => ({
  useAgentInstructions: () => ({ data: null }),
  useUpdateAgentInstructions: () => ({ mutate: vi.fn() }),
}));

describe("StreamingThinkingBlock", () => {
  const longContent = "Line one of thinking content.\n".repeat(20);

  it("renders with max-height constraint on content area", () => {
    const { container } = render(
      <StreamingThinkingBlock content={longContent} isActive={true} />
    );
    const contentArea = container.querySelector("[data-testid='thinking-content']");
    expect(contentArea).toBeInTheDocument();
    expect(contentArea).toHaveClass("max-h-[3.5rem]");
  });

  it("shows gradient fade overlay when content overflows", () => {
    const { container } = render(
      <StreamingThinkingBlock content={longContent} isActive={true} />
    );
    const fade = container.querySelector("[data-testid='thinking-fade']");
    expect(fade).toBeInTheDocument();
  });

  it("shows expand button that removes height constraint", () => {
    const { container } = render(
      <StreamingThinkingBlock content={longContent} isActive={true} />
    );
    const expandBtn = screen.getByRole("button", { name: /expand/i });
    expect(expandBtn).toBeInTheDocument();
    fireEvent.click(expandBtn);
    const contentArea = container.querySelector("[data-testid='thinking-content']");
    expect(contentArea).not.toHaveClass("max-h-[3.5rem]");
  });

  it("shows 'Thinking...' label when active", () => {
    render(<StreamingThinkingBlock content="test" isActive={true} />);
    expect(screen.getByText("Thinking...")).toBeInTheDocument();
  });

  it("shows 'Thought process' label when inactive", () => {
    render(<StreamingThinkingBlock content="test" isActive={false} />);
    expect(screen.getByText("Thought process")).toBeInTheDocument();
  });

  it("shows expand button even for short content", () => {
    render(<StreamingThinkingBlock content="Short thought." isActive={true} />);
    const expandBtn = screen.queryByRole("button", { name: /expand/i });
    expect(expandBtn).toBeInTheDocument();
  });
});

describe("ThinkingBlock (completed)", () => {
  const longContent = "Completed thinking content.\n".repeat(20);

  it("renders collapsed by default", () => {
    const { container } = render(<ThinkingBlock content={longContent} />);
    const details = container.querySelector("details");
    expect(details).toBeInTheDocument();
    expect(details).not.toHaveAttribute("open");
  });

  it("expands on click and shows content with max-height", () => {
    const { container } = render(<ThinkingBlock content={longContent} />);
    const summary = container.querySelector("summary");
    fireEvent.click(summary!);
    const contentArea = container.querySelector("[data-testid='thinking-content']");
    expect(contentArea).toBeInTheDocument();
    expect(contentArea).toHaveClass("max-h-[3.5rem]");
  });

  it("has a 'Show more' toggle to expand full content", () => {
    const { container } = render(<ThinkingBlock content={longContent} />);
    const summary = container.querySelector("summary");
    fireEvent.click(summary!);
    const showMore = screen.getByRole("button", { name: /show more/i });
    fireEvent.click(showMore);
    const contentArea = container.querySelector("[data-testid='thinking-content']");
    expect(contentArea).not.toHaveClass("max-h-[3.5rem]");
  });
});

describe("Streaming section layout", () => {
  it("StatusHeadline shows the latest running step label", () => {
    const steps = [
      { label: "Querying NetSuite...", status: "complete" as const },
      { label: "Analyzing results...", status: "running" as const },
    ];
    render(<StatusHeadline steps={steps} />);
    expect(screen.getByText("Analyzing results...")).toBeInTheDocument();
  });

  it("StatusHeadline shows last completed step with checkmark when nothing running", () => {
    const steps = [
      { label: "Querying NetSuite...", status: "complete" as const },
      { label: "Found 24 rows", status: "complete" as const },
    ];
    render(<StatusHeadline steps={steps} />);
    expect(screen.getByText("Found 24 rows")).toBeInTheDocument();
  });

  it("StatusHeadline renders nothing when no steps", () => {
    const { container } = render(<StatusHeadline steps={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
