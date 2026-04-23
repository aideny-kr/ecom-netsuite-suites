import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { CitationRenderer } from "../citation-renderer";

describe("CitationRenderer", () => {
  it("replaces bracketed source names with clickable links", () => {
    render(
      <CitationRenderer
        text="Our return window is 30 days [Returns Policy]."
        sources={{ "Returns Policy": "https://docs.google.com/document/d/xyz/edit" }}
      />
    );
    const link = screen.getByRole("link", { name: "Returns Policy" });
    expect(link).toHaveAttribute("href", "https://docs.google.com/document/d/xyz/edit");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringMatching(/noopener/i));
  });

  it("renders unmatched bracketed text as plain text", () => {
    render(
      <CitationRenderer
        text="See [Unknown Source] for details."
        sources={{ "Returns Policy": "https://x" }}
      />
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/See \[Unknown Source\] for details\./)).toBeInTheDocument();
  });

  it("does not convert markdown links like [text](url)", () => {
    render(
      <CitationRenderer
        text='See [here](https://example.com) for more.'
        sources={{ here: "https://x" }}
      />
    );
    expect(screen.queryByRole("link", { name: "here" })).toBeNull();
  });

  it("renders plain text when no sources map provided", () => {
    render(<CitationRenderer text="No citations here [x]." sources={undefined} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/No citations here \[x\]\./)).toBeInTheDocument();
  });

  it("handles multiple citations in one string", () => {
    render(
      <CitationRenderer
        text="See [Docs] and [FAQ]."
        sources={{ Docs: "https://a", FAQ: "https://b" }}
      />
    );
    expect(screen.getByRole("link", { name: "Docs" })).toHaveAttribute("href", "https://a");
    expect(screen.getByRole("link", { name: "FAQ" })).toHaveAttribute("href", "https://b");
  });
});
