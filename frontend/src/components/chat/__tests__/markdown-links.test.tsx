/**
 * Chat markdown links open in a NEW tab.
 *
 * Motivating case: the record link appended after a successful NetSuite write
 * ("View customer 5803124 in NetSuite"). Following it in the same tab
 * navigates the user out of the conversation they are mid-way through —
 * losing the confirmation card and the thread — to glance at a record.
 *
 * rel="noopener noreferrer" is not decoration: these point at an external ERP,
 * and without noopener the opened page receives a handle on window.opener.
 *
 * Asserts against the exported `mdComponents` — the same map the message list
 * renders with — so this cannot pass while production drifts.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { mdComponents } from "@/components/chat/message-list";

function renderMd(content: string) {
  return render(
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
      {content}
    </ReactMarkdown>,
  );
}

describe("chat markdown links", () => {
  it("opens a NetSuite record link in a new tab", () => {
    renderMd(
      "[View customer 5803124 in NetSuite](https://6738075.app.netsuite.com/app/common/entity/custjob.nl?id=5803124)",
    );
    const link = screen.getByRole("link", { name: /View customer 5803124/ });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
    expect(link).toHaveAttribute(
      "href",
      "https://6738075.app.netsuite.com/app/common/entity/custjob.nl?id=5803124",
    );
  });

  it("applies to ordinary links too", () => {
    renderMd("[docs](https://example.com/x)");
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("target", "_blank");
  });
});
