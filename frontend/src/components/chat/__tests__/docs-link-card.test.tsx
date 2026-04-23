import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { DocsLinkCard } from "../docs-link-card";
import type { DocsLinkData } from "@/lib/chat-stream";

const base: DocsLinkData = {
  url: "https://docs.google.com/document/d/FID/edit",
  doc_id: "FID",
  title: "Q1 Research",
  shared_with: null,
};

describe("DocsLinkCard", () => {
  it("renders the title", () => {
    render(<DocsLinkCard data={base} />);
    expect(screen.getByText("Q1 Research")).toBeInTheDocument();
  });

  it("links to the document URL with target=_blank + rel noopener", () => {
    render(<DocsLinkCard data={base} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", base.url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("shows 'Shared with' when shared_with is set", () => {
    render(<DocsLinkCard data={{ ...base, shared_with: "user@example.com" }} />);
    expect(screen.getByText(/Shared with/)).toBeInTheDocument();
    expect(screen.getByText(/user@example.com/)).toBeInTheDocument();
  });

  it("falls back to the URL caption when not shared", () => {
    render(<DocsLinkCard data={base} />);
    expect(screen.queryByText(/Shared with/)).not.toBeInTheDocument();
    // URL appears in both href and caption — ensure caption render
    expect(screen.getByText(base.url)).toBeInTheDocument();
  });

  it("has an accessible name tying the link to the doc title", () => {
    render(<DocsLinkCard data={base} />);
    expect(
      screen.getByRole("link", { name: /open q1 research in google docs/i }),
    ).toBeInTheDocument();
  });
});
